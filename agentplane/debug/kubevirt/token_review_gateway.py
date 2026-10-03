"""Prototype HTTP endpoint proving relay-to-Kubernetes identity, not egress policy.

No request headers or bearer tokens are logged/returned. Synthetic responses name
only the reviewed account and bound Pod. Not the production credential gateway.
"""

import json
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
CONTEXT = ssl.create_default_context(cafile=str(TOKEN_DIR / "ca.crt"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        bearer = self.headers.get("Proxy-Authorization", "").removeprefix("Bearer ")
        if not bearer:
            self.send_error(407, "relay token required")
            return
        body = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": bearer, "audiences": ["agentplane-egress"]},
        }
        request = urllib.request.Request(
            "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + (TOKEN_DIR / "token").read_text().strip(),
            },
        )
        try:
            with urllib.request.urlopen(request, context=CONTEXT, timeout=5) as response:
                status = json.load(response)["status"]
        except OSError:
            self.send_error(503, "review unavailable")
            return
        if not status.get("authenticated"):
            self.send_error(403, "review denied")
            return
        user = status["user"]
        data = json.dumps(
            {
                "username": user["username"],
                "pod_uid": user.get("extra", {}).get("authentication.kubernetes.io/pod-uid", []),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
