"""Clears animepahe's Cloudflare challenge with a locally installed browser.

Cloudflare binds the ``cf_clearance`` cookie to the User-Agent of the browser that
solved the challenge, so the provider has to replay that exact User-Agent along with
the cookies. The result is cached so later runs start without a browser until
Cloudflare asks again.

Headless Firefox passes the challenge in a few seconds without ever showing a window,
so it is tried first. Headless Chromium browsers are always caught, so the fallback
runs one with a real, minimized window, which can still pull the user out of a
full-screen app on macOS.
"""

import http.server
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, TypedDict

import httpx

from .....core.constants import APP_CACHE_DIR
from .constants import ANIMEPAHE, ANIMEPAHE_BASE, ANIMEPAHE_ENDPOINT

logger = logging.getLogger(__name__)

CLEARANCE_FILE = APP_CACHE_DIR / "animepahe-clearance.json"

# Seconds to wait with the window hidden before showing it so the user can finish
# an interactive challenge, and the total wait before giving up.
HIDDEN_WAIT = 30
TOTAL_WAIT = 180
# Headless Firefox needs no interaction, so give up on it sooner.
FIREFOX_WAIT = 45

_MACOS_FIREFOX = [
    "Firefox.app/Contents/MacOS/firefox",
    "Firefox Developer Edition.app/Contents/MacOS/firefox",
    "Firefox Nightly.app/Contents/MacOS/firefox",
]
_WINDOWS_FIREFOX = [r"Mozilla Firefox\firefox.exe"]
_LINUX_FIREFOX = ["firefox", "firefox-esr"]

# Brave Origin is left out: every fresh profile opens on its activation screen.
_MACOS_BROWSERS = [
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
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


def _is_firefox(path: str) -> bool:
    return "firefox" in Path(path).name.lower()


def _find(macos: list[str], windows: list[str], linux: list[str]) -> Optional[str]:
    if sys.platform == "darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        candidates = [root / rel for rel in macos for root in roots]
    elif sys.platform == "win32":
        roots = [
            os.environ.get(var)
            for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        candidates = [Path(root) / rel for rel in windows for root in roots if root]
    else:
        candidates = [Path(p) for p in map(shutil.which, linux) if p]
    return next((str(c) for c in candidates if c.is_file()), None)


def find_firefox() -> Optional[str]:
    override = os.environ.get("VIU_BROWSER")
    if override:
        return override if _is_firefox(override) else None
    return _find(_MACOS_FIREFOX, _WINDOWS_FIREFOX, _LINUX_FIREFOX)


def find_browser() -> Optional[str]:
    """Find a Chromium-based browser for the windowed fallback."""
    override = os.environ.get("VIU_BROWSER")
    if override:
        return None if _is_firefox(override) else override
    return _find(_MACOS_BROWSERS, _WINDOWS_BROWSERS, _LINUX_BROWSERS)


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
    firefox = find_firefox()
    if firefox:
        try:
            return _solve_with_firefox(firefox)
        except CloudflareError as e:
            logger.warning(f"{e}; trying a Chromium-based browser instead")
    browser = find_browser()
    if not browser:
        raise CloudflareError(
            "animepahe is behind a Cloudflare challenge and no browser that can clear "
            "it was found. Install Firefox (or Chrome, Edge, Brave, Chromium, "
            "Vivaldi, Opera), or point VIU_BROWSER at one."
        )
    return _solve_with_chromium(browser)


def _firefox_cookies(profile: str) -> dict[str, str]:
    """Read animepahe's cookies from a running Firefox's cookie database."""
    database = Path(profile) / "cookies.sqlite"
    if not database.exists():
        return {}
    # Firefox holds the database open, so read a copy (with its write-ahead log).
    copy_dir = tempfile.mkdtemp(prefix="viu-cookies-")
    try:
        for suffix in ("", "-wal"):
            source = Path(f"{database}{suffix}")
            if source.exists():
                shutil.copy(source, Path(copy_dir) / f"cookies.sqlite{suffix}")
        connection = sqlite3.connect(Path(copy_dir) / "cookies.sqlite")
        try:
            rows = connection.execute(
                "SELECT name, value FROM moz_cookies WHERE host IN (?, ?)",
                (ANIMEPAHE, f".{ANIMEPAHE}"),
            ).fetchall()
        finally:
            connection.close()
        return dict(rows)
    except (OSError, sqlite3.Error):
        return {}
    finally:
        shutil.rmtree(copy_dir, ignore_errors=True)


def _solve_with_firefox(firefox: str) -> Clearance:
    # Firefox runs with no remote-control protocol, which Cloudflare would notice,
    # so a local page records its User-Agent and redirects it to animepahe.
    seen: dict[str, str] = {}

    class RecordUserAgent(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["user_agent"] = self.headers["User-Agent"]
            self.send_response(302)
            self.send_header("Location", f"{ANIMEPAHE_BASE}/")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RecordUserAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="viu-cloudflare-")
    (Path(profile) / "user.js").write_text(
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n'
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("app.update.enabled", false);\n'
    )
    process = subprocess.Popen(
        [
            firefox,
            "--headless",
            "--no-remote",
            "--profile",
            profile,
            f"http://127.0.0.1:{server.server_port}/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "MOZ_HEADLESS": "1"},
    )
    logger.info(f"Clearing animepahe's Cloudflare challenge with headless {firefox}")
    started = time.monotonic()
    try:
        with httpx.Client(http2=True, timeout=15) as client:
            while time.monotonic() - started < FIREFOX_WAIT:
                time.sleep(1)
                if process.poll() is not None:
                    raise CloudflareError(
                        f"{firefox} exited before clearing Cloudflare"
                    )
                cookies = _firefox_cookies(profile)
                if "cf_clearance" not in cookies or "user_agent" not in seen:
                    continue
                # Cloudflare sets cf_clearance before the check passes, so only a
                # real answer from the API proves the cookie works.
                response = client.get(
                    ANIMEPAHE_ENDPOINT,
                    params={"m": "search", "q": "naruto"},
                    headers={"User-Agent": seen["user_agent"]},
                    cookies=cookies,
                )
                if response.status_code == 200:
                    logger.info(
                        f"Cloudflare cleared in {time.monotonic() - started:.0f}s"
                    )
                    return {"user_agent": seen["user_agent"], "cookies": cookies}
        raise CloudflareError(
            f"headless Firefox did not clear Cloudflare within {FIREFOX_WAIT}s"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(profile, ignore_errors=True)


def _solve_with_chromium(browser: str) -> Clearance:
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
            break
        time.sleep(0.1)
    # Also catches a browser that was still starting when we gave up on it. The
    # throwaway profile path is unique, so this only ever matches our instance.
    subprocess.run(["pkill", "-f", profile], check=False)
