"""Local HTTP/1.1 relay for animepahe's stream CDN, which only answers HTTP/2.

Cloudflare in front of the CDN rejects HTTP/1.1 with a 403, and mpv, VLC and ffmpeg
only speak HTTP/1.1. The relay listens on localhost, fetches the upstream URL over
HTTP/2 with the stream headers, and rewrites HLS playlists so every key, segment and
variant URL also goes through it. It lives as long as the viu process.
"""

import base64
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')
_FORWARDED_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
)

_relay: Optional["StreamRelay"] = None
_relay_lock = threading.Lock()


def get_relay(headers: dict[str, str]) -> "StreamRelay":
    global _relay
    with _relay_lock:
        if _relay is None:
            _relay = StreamRelay(headers)
        else:
            _relay.client.headers.update(headers)
        return _relay


def _encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _decode(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()


class StreamRelay:
    def __init__(self, headers: dict[str, str]):
        self.client = httpx.Client(
            http2=True,
            headers={**headers, "Accept-Encoding": "identity"},
            follow_redirects=True,
            timeout=httpx.Timeout(30),
        )
        self.allowed_hosts: set[str] = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        logger.debug(f"Stream relay listening on port {self.server.server_port}")

    def url_for(self, upstream: str) -> str:
        self.allowed_hosts.add(urlparse(upstream).hostname or "")
        path = urlparse(upstream).path
        name = path.rsplit("/", 1)[-1] or "index"
        # ffmpeg refuses HLS segments with image extensions; the CDN disguises TS
        # segments as .jpg, so give them a neutral name.
        if not name.endswith((".m3u8", ".key")):
            name = "segment.ts"
        return f"http://127.0.0.1:{self.server.server_port}/{_encode(upstream)}/{name}"

    def rewrite_playlist(self, playlist: str, base: str) -> str:
        lines = []
        for line in playlist.splitlines():
            if line.startswith("#"):
                line = _URI_ATTRIBUTE.sub(
                    lambda m: f'URI="{self.url_for(urljoin(base, m.group(1)))}"', line
                )
            elif line.strip():
                line = self.url_for(urljoin(base, line.strip()))
            lines.append(line)
        return "\n".join(lines) + "\n"


def _handler_for(relay: StreamRelay):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                upstream = _decode(self.path.lstrip("/").split("/", 1)[0])
            except ValueError:
                return self.send_error(400)
            if urlparse(upstream).hostname not in relay.allowed_hosts:
                return self.send_error(403)

            headers = {"Range": self.headers["Range"]} if self.headers["Range"] else {}
            try:
                with relay.client.stream("GET", upstream, headers=headers) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if "mpegurl" in content_type or upstream.endswith(".m3u8"):
                        response.read()
                        body = relay.rewrite_playlist(
                            response.text, str(response.url)
                        ).encode()
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    self.send_response(response.status_code)
                    for name in _FORWARDED_HEADERS:
                        if name in response.headers:
                            self.send_header(name, response.headers[name])
                    self.end_headers()
                    for chunk in response.iter_raw():
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player closed the connection, e.g. on seek
            except httpx.HTTPError as e:
                logger.error(f"Stream relay failed for {upstream}: {e}")
                self.send_error(502)

        def log_message(self, format, *args):
            logger.debug(f"relay: {format % args}")

    return Handler
