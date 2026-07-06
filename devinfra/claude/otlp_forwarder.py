"""Localhost OTLP relay for Claude Code web sessions.

Claude Code's native OTel exporter cannot reach the cluster directly: the
managed container silently drops the claude process's direct HTTPS egress, and
the Node exporter ignores HTTPS_PROXY (verified 2026-07-06 — see
plans/transcript_collection.md). So the session env points
OTEL_EXPORTER_OTLP_ENDPOINT at this relay, which forwards to the
Authentik-proxied Alloy endpoint through the container's egress proxy and
attaches the rotated bearer.

The bearer is re-read from TOKEN_FILE on every request so rotation propagates
without a restart; ensure_otel_forwarder.sh materializes the file. Requests
arriving before the token exists get 503 — the exporter retries/drops and the
session is unaffected.
"""

import logging
import os
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN_ADDR = ("127.0.0.1", 4318)
UPSTREAM = os.environ.get("OTLP_FORWARD_UPSTREAM", "https://alloy-otlp.allegedly.works")
TOKEN_FILE = Path.home() / ".cache" / "ducktape" / "otel-bearer"
# Headers that carry OTLP payload semantics end-to-end; everything else is hop-local.
FORWARD_HEADERS = ("content-type", "content-encoding")

logger = logging.getLogger(__name__)


def ssl_context() -> ssl.SSLContext:
    # The egress proxy TLS-intercepts; its CA is published in the container.
    for var in ("SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        if (cafile := os.environ.get(var)) and Path(cafile).exists():
            return ssl.create_default_context(cafile=cafile)
    ccr_ca = Path("/root/.ccr/ca-bundle.crt")
    if ccr_ca.exists():
        return ssl.create_default_context(cafile=str(ccr_ca))
    return ssl.create_default_context()


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    relayed = 0

    def read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def respond(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:
        body = self.read_body()
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            self.respond(503)
            return
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        for name in FORWARD_HEADERS:
            if value := self.headers.get(name):
                req.add_header(name, value)
        try:
            # urllib honors HTTPS_PROXY from the environment.
            with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
            logger.warning("upstream %s -> HTTP %s", self.path, e.code)
        except (urllib.error.URLError, OSError) as e:
            logger.warning("upstream %s unreachable: %s", self.path, e)
            self.respond(502)
            return
        Relay.relayed += 1
        if Relay.relayed % 100 == 1:
            logger.info("relayed %d requests (last: %s -> %s)", Relay.relayed, self.path, code)
        self.respond(code)

    def log_message(self, format: str, *args: object) -> None:
        pass  # per-request stderr noise; the heartbeat above is the signal


CTX = ssl_context()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("otlp forwarder listening on %s:%d -> %s", *LISTEN_ADDR, UPSTREAM)
    ThreadingHTTPServer(LISTEN_ADDR, Relay).serve_forever()


if __name__ == "__main__":
    main()
