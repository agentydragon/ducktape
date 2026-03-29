"""Test client for k8s proxy integration test.

Runs inside a container on the proxy network. Reads a k8s secret through
mitmproxy using the kubernetes Python client with explicit Proxy-Authorization
header (normalize_proxy_url pattern). Prints the result as JSON to stdout.

Environment variables:
  PROXY_URL: mitmproxy URL with embedded credentials
  K8S_SERVER: mock k8s API URL (CONNECT target)
  CA_FILE: path to mitmproxy CA cert
"""

import base64
import json
import os
from urllib.parse import urlparse

from kubernetes import client as k8s_client
from kubernetes.client import Configuration, CoreV1Api


def main() -> None:
    proxy_url = os.environ["PROXY_URL"]
    k8s_server = os.environ["K8S_SERVER"]
    ca_file = os.environ["CA_FILE"]

    # normalize_proxy_url: split creds into Proxy-Authorization header.
    # Duplicated here (not imported) because this runs in a standalone container.
    parsed = urlparse(proxy_url)
    if parsed.username:
        password = parsed.password or ""
        auth = base64.b64encode(f"{parsed.username}:{password}".encode()).decode()
        proxy_headers = {"Proxy-Authorization": f"Basic {auth}"}
        netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        clean_proxy = parsed._replace(netloc=netloc).geturl()
    else:
        clean_proxy = proxy_url
        proxy_headers = {}

    cfg = Configuration()
    cfg.host = k8s_server
    cfg.api_key = {"authorization": "Bearer test-token"}
    cfg.ssl_ca_cert = ca_file
    cfg.proxy = clean_proxy
    if proxy_headers:
        cfg.proxy_headers = proxy_headers

    api = CoreV1Api(k8s_client.ApiClient(cfg))
    secret = api.read_namespaced_secret("github-token", "test-ns")
    token = base64.b64decode(secret.data["token"]).decode()

    print(json.dumps({"token": token}))


if __name__ == "__main__":
    main()
