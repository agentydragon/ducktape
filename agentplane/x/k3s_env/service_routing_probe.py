import json
import os
import socket
import ssl
import urllib.request
from pathlib import Path


def main() -> None:
    host = "kubernetes.default.svc.cluster.local"
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
    ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    request = urllib.request.Request(f"https://{host}/version", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=ca), timeout=10) as response:
        version = json.load(response)["gitVersion"]
    print(
        json.dumps(
            {
                "dns_ip": socket.gethostbyname(host),
                "service_host": os.environ["KUBERNETES_SERVICE_HOST"],
                "service_port": os.environ["KUBERNETES_SERVICE_PORT"],
                "api_version": version,
            }
        )
    )


if __name__ == "__main__":
    main()
