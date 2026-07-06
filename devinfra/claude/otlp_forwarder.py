"""Localhost OTLP relay for Claude Code web sessions.

Claude Code's native OTel exporter cannot reach the cluster directly: the
managed container silently drops the claude process's direct HTTPS egress
(verified 2026-07-06 — see plans/transcript_collection.md). So the session env
points OTEL_EXPORTER_OTLP_ENDPOINT at this relay, which forwards to the
Authentik-proxied Alloy endpoint through the container's egress proxy and
attaches the rotated bearer.

The bearer is re-read from TOKEN_FILE on every request so rotation propagates
without a restart; ensure_otel_forwarder.sh materializes the file. Requests
arriving before the token exists get 503 — the exporter retries/drops and the
session is unaffected.
"""

import errno
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


class BodyError(Exception):
    """Request body could not be framed; carries the HTTP status to answer with."""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    relayed = 0
    warned_no_token = False

    def read_body(self) -> bytes:
        # The live exporter sends chunked bodies (observed: POSTs with no
        # Content-Length), so this branch is load-bearing, not speculative.
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline()
                try:
                    size = int(line.strip().split(b";")[0], 16)
                except ValueError as e:
                    raise BodyError(400, f"bad chunk-size line {line!r}") from e
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        if (length := self.headers.get("Content-Length")) is None:
            # Neither framing — reading to EOF would desync keep-alive; refuse.
            raise BodyError(411, "no Content-Length and not chunked")
        return self.rfile.read(int(length))

    def respond(self, code: int, close: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "2")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:
        try:
            body = self.read_body()
        except BodyError as e:
            # After a framing error the connection byte-stream is unreliable;
            # close it so leftover bytes can't be parsed as the next request.
            logger.warning("bad request %s: %s", self.path, e)
            self.respond(e.code, close=True)
            return
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            if not Relay.warned_no_token:
                Relay.warned_no_token = True
                logger.warning("no bearer at %s yet; 503ing until it appears", TOKEN_FILE)
            self.respond(503)
            return
        Relay.warned_no_token = False
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
            logger.warning("upstream %s unreachable: %r", self.path, e)
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
    try:
        server = ThreadingHTTPServer(LISTEN_ADDR, Relay)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            # Lost the startup race against a concurrent launch — that's fine,
            # exactly one forwarder is what we want.
            logger.info("port %d already bound; another forwarder is running", LISTEN_ADDR[1])
            return
        raise
    logger.info("otlp forwarder listening on %s:%d -> %s", *LISTEN_ADDR, UPSTREAM)
    server.serve_forever()


if __name__ == "__main__":
    main()
