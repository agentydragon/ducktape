"""Localhost-only receiver that turns POST /reseed bodies into tana:// deep-links.

The firebase_resigner sidecar POSTs `{"url": "tana://auth?token=...&providerId=tanaFirebaseToken"}`
here. We exec the Tana binary with that URL as argv[1], which triggers
Electron's `second-instance` handler in the already-running Tana process,
which dispatches the `auth` IPC into the renderer and completes
`signInWithCustomToken(...)`.

Listens on 127.0.0.1:9090 — pod loopback is shared between sidecar and
desktop containers but not reachable from outside the pod.
"""

import json
import logging
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9090
TANA_BIN = "/opt/tana/Tana"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s reseed: %(message)s")
logger = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    # Silence default per-request access logs — we log explicitly on actions.
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        if self.path != "/reseed":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            url = payload["url"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self.send_error(400, f"bad body: {e}")
            return
        if not isinstance(url, str) or not url.startswith("tana://"):
            self.send_error(400, "url must be a tana:// scheme string")
            return
        # No-sandbox + disable-gpu mirrors what entrypoint.sh launches Tana
        # with. We spawn a second-instance — Electron's existing single-
        # instance lock routes the URL into the running renderer.
        logger.info(f"reseed: delivering {url[:32]}…")
        subprocess.Popen(
            [TANA_BIN, "--no-sandbox", "--disable-gpu", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.send_response(202)
        self.end_headers()


def main() -> None:
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    logger.info(f"listening on http://{LISTEN_HOST}:{LISTEN_PORT}/reseed")
    server.serve_forever()


if __name__ == "__main__":
    main()
