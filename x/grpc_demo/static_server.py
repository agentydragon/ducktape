"""Serve the Bazel-built gRPC-Web demo bundle."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from python.runfiles import runfiles


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

    handler = partial(SimpleHTTPRequestHandler, directory=static_dir)
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
