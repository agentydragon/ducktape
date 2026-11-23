"""Run stdlib HTTP servers for tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from threading import Thread


@contextmanager
def serve_http_in_thread[Server: HTTPServer](server: Server) -> Iterator[Server]:
    """Serve on the already-bound socket until the block exits, then close it."""
    with server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
