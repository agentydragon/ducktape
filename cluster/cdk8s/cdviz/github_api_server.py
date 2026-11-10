"""Tiny paginated GitHub Actions API fixture for the CDviz integration test."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit


class GitHubFixtureServer(ThreadingHTTPServer):
    requested_pages: list[int]

    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 8081), GitHubFixtureHandler)
        self.requested_pages = []


class GitHubFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        server = cast(GitHubFixtureServer, self.server)
        if parsed.path == "/requests":
            self._send_json(200, server.requested_pages)
            return
        if parsed.path == "/repos/agentydragon/ducktape/rules/branches/devel":
            self._send_json(
                200,
                [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [
                                {"context": "bazel-ci / Test & Build", "integration_id": 15368},
                                {"context": "Pre-commit checks", "integration_id": 15368},
                                {"context": "Build artifacts + imports check", "integration_id": 15368},
                            ]
                        },
                    }
                ],
            )
            return

        query = parse_qs(parsed.query)
        if query.get("per_page") != ["100"]:
            self._send_json(400, {"error": "expected per_page=100"})
            return
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            self._send_json(400, {"error": "invalid page"})
            return

        server.requested_pages.append(page)
        filename = "recent.json" if page == 1 else "payload.json"
        body = (Path("/fixture") / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        next_url = f"http://{self.headers['Host']}{parsed.path}?per_page=100&page={page + 1}"
        self.send_header("Link", f'<{next_url}>; rel="next"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with GitHubFixtureServer() as server:
        server.serve_forever()
