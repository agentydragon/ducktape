# OCI Helpers for Agent Image Authoring

## Status: Not Started

## Problem

PO/PI agents that author other agents need to manipulate OCI images via the registry proxy. The current test (test_e2e.py) shows this requires ~150 lines of inline Python code for operations like creating tar.gz layers, calculating digests, and handling auth.

Agents understand Docker/OCI concepts well, but certain operations are tedious:

- Creating properly formatted OCI layer tarballs (tar.gz with correct structure)
- Calculating sha256 digests in OCI format ("sha256:...")
- Getting registry auth credentials from environment variables

## Goals

Provide simple helpers that:

1. Handle tedious operations that aren't practical with curl alone
2. Mirror standard OCI terminology and concepts
3. Work as both Python library (for inline code) and CLI commands (for docker_exec)
4. Support only local registry (no external registries)

## Non-Goals

- High-level "do everything" commands (agents compose primitives themselves)
- Layer inspection/extraction (agents don't need this)
- Support for external registries (docker.io, etc.)
- Image building from Dockerfiles (only layer manipulation)

## Design

### Python API

Location: `props/core/oci_helpers.py`

```python
from props.core.oci_helpers import (
    get_registry_auth,      # () -> tuple[str, str]
    get_registry_url,       # () -> str
    create_layer_tar,       # (files: dict[str, bytes]) -> bytes
    calculate_digest,       # (content: bytes) -> str
)
```

**Function details:**

```python
def get_registry_auth() -> tuple[str, str]:
    """Get registry auth credentials from environment.

    Returns:
        (username, password) tuple from PGUSER and PGPASSWORD env vars

    Raises:
        ValueError: If env vars not set
    """

def get_registry_url() -> str:
    """Get registry proxy URL from environment.

    Returns:
        URL like "http://registry-proxy:5050"

    Uses PROPS_REGISTRY_PROXY_HOST (default: "registry-proxy")
    and PROPS_REGISTRY_PROXY_PORT (default: "5050")
    """

def create_layer_tar(files: dict[str, bytes]) -> bytes:
    """Create OCI layer tar.gz from files.

    Args:
        files: Map of image path -> file content
               Example: {"agent.md": b"# My Agent\\n..."}

    Returns:
        Gzip-compressed tar bytes suitable for OCI layer

    The tar is created with:
    - Deterministic ordering (sorted by path)
    - No timestamps (for reproducibility)
    - Proper permissions (0644 for files)
    """

def calculate_digest(content: bytes) -> str:
    """Calculate OCI digest for content.

    Args:
        content: Bytes to hash (layer tar.gz, manifest JSON, etc.)

    Returns:
        Digest in OCI format: "sha256:abc123..."
    """
```

### CLI Commands

Location: `props/core/cli/cmd_oci.py`

```bash
# Auth credentials
props oci auth
# Output: username:password

# Registry URL
props oci registry-url
# Output: http://registry-proxy:5050

# Create layer tar.gz
props oci create-layer <path1>:<content1> [<path2>:<content2> ...]
# Output: tar.gz bytes to stdout
# Example: props oci create-layer agent.md:./my-agent.md > layer.tar.gz

# Calculate digest
props oci digest [file]
# Output: sha256:abc123...
# If file omitted, reads stdin
```

## Example: Simplified test_e2e.py

**Before (lines 85-200): 115 lines of Python in docker_exec**

```python
create_and_push_script = textwrap.dedent(f"""
    import os, json, hashlib, tarfile, gzip, tempfile, requests
    from requests.auth import HTTPBasicAuth
    from io import BytesIO

    auth = HTTPBasicAuth(os.environ['PGUSER'], os.environ['PGPASSWORD'])
    proxy_url = f"http://{{proxy_host}}:{{proxy_port}}"

    # 30 lines: Create tar.gz layer manually
    tar_buffer = BytesIO()
    with gzip.open(tar_buffer, 'wb') as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            info = tarfile.TarInfo(name='agent.md')
            info.size = len(agent_md_content.encode('utf-8'))
            tar.addfile(info, BytesIO(agent_md_content.encode('utf-8')))
    layer_blob = tar_buffer.getvalue()
    layer_digest = "sha256:" + hashlib.sha256(layer_blob).hexdigest()

    # 20 lines: Upload blob via OCI Distribution API
    upload_url = f"{{proxy_url}}/v2/critic/blobs/uploads/"
    resp = requests.post(upload_url, auth=auth, timeout=10)
    ...

    # 30 lines: Create and push manifest
    manifest = {{...}}
    manifest_json = json.dumps(manifest, separators=(',', ':'), sort_keys=True)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_json.encode()).hexdigest()
    ...
""")
```

**After (with helpers): ~40 lines**

```python
create_and_push_script = textwrap.dedent(f"""
    import json, requests
    from props.core.oci_helpers import (
        get_registry_auth, get_registry_url,
        create_layer_tar, calculate_digest
    )

    username, password = get_registry_auth()
    auth = (username, password)
    proxy_url = get_registry_url()

    # Create layer
    agent_md = '''# Custom Critic - {random_token}...'''
    layer_blob = create_layer_tar({{"agent.md": agent_md.encode()}})
    layer_digest = calculate_digest(layer_blob)

    # Upload blob
    resp = requests.post(f"{{proxy_url}}/v2/critic/blobs/uploads/", auth=auth)
    upload_loc = resp.headers['Location']
    requests.put(f"{{proxy_url}}{{upload_loc}}&digest={{layer_digest}}",
                 data=layer_blob, auth=auth)

    # Build and push manifest
    manifest = {{
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {{"digest": "sha256:placeholder", "size": 123}},
        "layers": [{{"digest": layer_digest, "size": len(layer_blob)}}]
    }}
    manifest_json = json.dumps(manifest, separators=(',', ':'))
    manifest_digest = calculate_digest(manifest_json.encode())
    requests.put(f"{{proxy_url}}/v2/critic/manifests/{{manifest_digest}}",
                 data=manifest_json,
                 headers={{"Content-Type": "application/vnd.oci.image.manifest.v1+json"}},
                 auth=auth)
    print(f"MANIFEST_DIGEST={{manifest_digest}}")
""")
```

## Implementation Tasks

1. ✅ Design Python API (this document)
2. ❌ Implement `props/core/oci_helpers.py`
3. ❌ Add unit tests for helpers
4. ❌ Implement CLI commands in `props/core/cli/cmd_oci.py`
5. ❌ Refactor test_e2e.py to use helpers
6. ❌ Update authoring_agents.md.j2 with examples using helpers

## Notes

- Helpers available in all agent containers (props CLI installed at /app/)
- Agents can use Python API directly or CLI via docker_exec
- No need to support layer extraction/inspection (agents only create layers)
- Registry operations (GET/PUT manifests) remain agent's responsibility (simple HTTP)
- Manifest structure knowledge remains with agent (they understand OCI)

## References

- Test code: `props/core/tests/agent_pkg/test_e2e.py` lines 85-200
- Agent authoring guide: `props/core/docs/authoring_agents.md.j2`
- OCI Distribution Spec: https://github.com/opencontainers/distribution-spec
