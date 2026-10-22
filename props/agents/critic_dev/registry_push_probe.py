"""Report registry proxy responses to a critic container e2e test."""

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request

CONTAINER_SCRIPT_PATH = "/tmp/registry_push_probe.py"


def _status(auth: str, method: str, path: str, body: bytes | None = None) -> int:
    url = os.environ["PROPS_REGISTRY_URL"] + path
    request = urllib.request.Request(url, data=body, method=method, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


def main() -> None:
    auth = base64.b64encode(f"{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}".encode()).decode()
    manifest = b'{"schemaVersion": 2}'
    digest = hashlib.sha256(manifest).hexdigest()
    statuses = {
        "api_check": _status(auth, "GET", "/v2/"),
        "blob_upload": _status(auth, "POST", "/v2/critic/blobs/uploads/", b""),
        "manifest_put": _status(auth, "PUT", f"/v2/critic/manifests/sha256:{digest}", manifest),
    }
    print(json.dumps(statuses, sort_keys=True))


if __name__ == "__main__":
    main()
