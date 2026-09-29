"""Clears animepahe's Cloudflare challenge with a local Chromium-family browser.

Cloudflare binds the ``cf_clearance`` cookie to the User-Agent of the browser that
solved the challenge, so the provider has to replay that exact User-Agent along with
the cookies. Headless browsers never pass the challenge; a normal, minimized window
does, usually in under ten seconds. The browser is started in the background so it
never takes focus or pulls the user out of a full-screen app, and the result is
cached so later runs start without a browser until Cloudflare asks again.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, TypedDict

from .....core.constants import APP_CACHE_DIR
from .constants import ANIMEPAHE, ANIMEPAHE_BASE

logger = logging.getLogger(__name__)

CLEARANCE_FILE = APP_CACHE_DIR / "animepahe-clearance.json"

# Seconds to wait with the window hidden before showing it so the user can finish
# an interactive challenge, and the total wait before giving up.
HIDDEN_WAIT = 30
TOTAL_WAIT = 180

_MACOS_BROWSERS = [
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
    "Brave Origin.app/Contents/MacOS/Brave Origin",
    "Vivaldi.app/Contents/MacOS/Vivaldi",
    "Opera.app/Contents/MacOS/Opera",
    "Opera GX.app/Contents/MacOS/Opera",
]
_WINDOWS_BROWSERS = [
    r"Google\Chrome\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Vivaldi\Application\vivaldi.exe",
]
_LINUX_BROWSERS = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
    "vivaldi",
]

_lock = threading.Lock()


class Clearance(TypedDict):
    user_agent: str
    cookies: dict[str, str]


class CloudflareError(Exception):
    pass


def load_clearance() -> Optional[Clearance]:
    try:
        return json.loads(CLEARANCE_FILE.read_text())
    except (OSError, ValueError):
        return None


def refresh_clearance(stale: Optional[Clearance]) -> Clearance:
    """Solve the challenge again, unless another thread already replaced ``stale``."""
    with _lock:
        current = load_clearance()
        if current and current != stale:
            return current
        clearance = _solve()
        CLEARANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLEARANCE_FILE.write_text(json.dumps(clearance))
        return clearance


def find_browser() -> Optional[str]:
    override = os.environ.get("VIU_BROWSER")
    if override:
        return override
    if sys.platform == "darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        candidates = [root / rel for rel in _MACOS_BROWSERS for root in roots]
    elif sys.platform == "win32":
        roots = [
            os.environ.get(var)
            for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        candidates = [
            Path(root) / rel for rel in _WINDOWS_BROWSERS for root in roots if root
        ]
    else:
        candidates = [Path(p) for p in map(shutil.which, _LINUX_BROWSERS) if p]
    return next((str(c) for c in candidates if c.is_file()), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _devtools_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
        return json.load(r)


class _DevTools:
    """Minimal synchronous Chrome DevTools Protocol client for the browser target."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect

        self._ws = connect(ws_url, max_size=None, open_timeout=10)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv(timeout=10))
            if message.get("id") == self._id:
                if "error" in message:
                    raise CloudflareError(f"{method}: {message['error']}")
                return message.get("result", {})

    def close(self):
        self._ws.close()


def _launch(browser: str, args: list[str]) -> Optional[subprocess.Popen]:
    app = Path(browser).parents[2]
    if sys.platform == "darwin" and app.suffix == ".app":
        # A browser started directly activates itself, and macOS then switches away
        # from whatever full-screen Space the user is in. `open -g` launches it
        # without activating it; -n keeps it apart from the user's own instance.
        subprocess.run(
            ["open", "-n", "-g", "-a", str(app), "--args", *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return None
    return subprocess.Popen(
        [browser, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _solve() -> Clearance:
    browser = find_browser()
    if not browser:
        raise CloudflareError(
            "animepahe is behind a Cloudflare challenge and no Chromium-based browser "
            "(Chrome, Edge, Brave, Chromium, Vivaldi, Opera) was found to clear it. "
            "Install one or point VIU_BROWSER at its executable."
        )

    port = _free_port()
    profile = tempfile.mkdtemp(prefix="viu-cloudflare-")
    process = _launch(
        browser,
        [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # Headless is always caught by Cloudflare, so use a real window placed
            # as far off-screen as the OS allows, then minimize it.
            "--window-position=-32000,-32000",
            "--window-size=800,600",
        ],
    )
    devtools = None
    logger.info(f"Clearing animepahe's Cloudflare challenge with {browser}")
    try:
        for _ in range(100):
            try:
                version = _devtools_json(port, "/json/version")
                break
            except OSError:
                if process and process.poll() is not None:
                    raise CloudflareError(f"{browser} exited before it could start")
                time.sleep(0.1)
        else:
            raise CloudflareError(f"{browser} did not open its DevTools port")

        devtools = _DevTools(version["webSocketDebuggerUrl"])
        # Some browsers ignore a start URL on a fresh profile, so open the tab here.
        target = devtools.call(
            "Target.createTarget", url=f"{ANIMEPAHE_BASE}/", background=True
        )
        for page in _devtools_json(port, "/json"):
            if page.get("type") == "page" and page["id"] != target["targetId"]:
                devtools.call("Target.closeTarget", targetId=page["id"])
        window = devtools.call(
            "Browser.getWindowForTarget", targetId=target["targetId"]
        )
        devtools.call(
            "Browser.setWindowBounds",
            windowId=window["windowId"],
            bounds={"windowState": "minimized"},
        )

        started = time.monotonic()
        shown = False
        while time.monotonic() - started < TOTAL_WAIT:
            time.sleep(1)
            pages = [
                p
                for p in _devtools_json(port, "/json")
                if p.get("id") == target["targetId"]
            ]
            title = pages[0]["title"] if pages else ""
            # The challenge page is titled "Just a moment..."; a cf_clearance cookie
            # alone proves nothing, since Cloudflare sets one before the check passes.
            if (
                title
                and "just a moment" not in title.lower()
                and ANIMEPAHE not in title
            ):
                cookies = devtools.call("Storage.getCookies")["cookies"]
                jar = {
                    c["name"]: c["value"]
                    for c in cookies
                    if c["domain"].lstrip(".") == ANIMEPAHE
                }
                if "cf_clearance" in jar:
                    logger.info(
                        f"Cloudflare cleared in {time.monotonic() - started:.0f}s"
                    )
                    return {"user_agent": version["User-Agent"], "cookies": jar}
            if not shown and time.monotonic() - started > HIDDEN_WAIT:
                # Cloudflare wants a click. Bring the window forward for the user.
                logger.warning(
                    "Cloudflare needs a manual check; complete it in the browser window"
                )
                devtools.call(
                    "Browser.setWindowBounds",
                    windowId=window["windowId"],
                    bounds={"windowState": "normal"},
                )
                devtools.call(
                    "Browser.setWindowBounds",
                    windowId=window["windowId"],
                    bounds={"left": 100, "top": 100, "width": 800, "height": 600},
                )
                devtools.call("Target.activateTarget", targetId=target["targetId"])
                shown = True
        raise CloudflareError(
            f"Cloudflare challenge was not cleared within {TOTAL_WAIT}s"
        )
    finally:
        if devtools:
            try:
                devtools.call("Browser.close")
            except Exception:
                pass
            devtools.close()
        if process:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            _wait_for_exit(port, profile)
        shutil.rmtree(profile, ignore_errors=True)


def _wait_for_exit(port: int, profile: str) -> None:
    """Make sure a browser started through `open` has quit."""
    for _ in range(50):
        try:
            _devtools_json(port, "/json/version")
        except OSError:
            return
        time.sleep(0.1)
    # The throwaway profile path is unique, so this only matches our instance.
    subprocess.run(["pkill", "-f", profile], check=False)
