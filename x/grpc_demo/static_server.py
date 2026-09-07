"""Serve the Bazel-built gRPC-Web demo bundle."""

from __future__ import annotations

import argparse
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from python.runfiles import runfiles


class DemoRequestHandler(SimpleHTTPRequestHandler):
    """Serve static files and non-secret runtime configuration."""

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/config.json":
            payload = json.dumps(
                {
                    "grpc_web_endpoint": os.environ.get("GRPC_DEMO_GRPC_WEB_ENDPOINT", "http://127.0.0.1:8080"),
                    "oidc_authority": os.environ.get("GRPC_DEMO_OIDC_AUTHORITY", ""),
                    "oidc_client_id": os.environ.get("GRPC_DEMO_OIDC_CLIENT_ID", ""),
                    "oidc_scope": os.environ.get("GRPC_DEMO_OIDC_SCOPE", "openid profile email"),
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8081, type=int)
    args = parser.parse_args()

    runfiles_dir = runfiles.Create()
    if runfiles_dir is None:
        raise RuntimeError("Bazel runfiles are required")
    static_dir = runfiles_dir.Rlocation("_main/x/grpc_demo/dist")
    if static_dir is None:
        raise RuntimeError("Bazel bundle directory is missing from runfiles")

    handler = partial(DemoRequestHandler, directory=static_dir)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {static_dir} at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
