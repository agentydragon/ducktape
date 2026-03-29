"""Standalone mock k8s API HTTPS server for integration testing.

Generates a self-signed cert and serves k8s Secret API responses.
Runs as a Bazel-built OCI container image.

Usage: mock_k8s_server_bin <secrets_json> <port>
  secrets_json: JSON dict of {secret_name: {key: value, ...}}
  port: TCP port to listen on
"""

import base64
import http.server
import json
import ssl
import sys
import tempfile
from pathlib import Path

from devinfra.claude.testing.proxy_ca import generate_server_cert


def main() -> None:
    secrets: dict[str, dict[str, str]] = json.loads(sys.argv[1])
    port = int(sys.argv[2])

    td = Path(tempfile.mkdtemp())
    cert_pem, key_pem = generate_server_cert("mock-k8s")
    cert_file = td / "cert.pem"
    key_file = td / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) == 6 and parts[4] == "secrets":
                name = parts[5]
                data = secrets.get(name, {"key": "default-value"})
                encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
                body = json.dumps({"apiVersion": "v1", "kind": "Secret", "data": encoded}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_file), str(key_file))
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"mock-k8s listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
