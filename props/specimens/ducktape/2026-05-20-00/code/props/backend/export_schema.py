"""Export OpenAPI schema from FastAPI app to stdout."""

import json

from props.backend.app import BackendDeps, create_app
from props.config import PropsConfig
from props.core.oci_utils import RegistryProxyConfig

if __name__ == "__main__":
    # Dummy deps — schema export only needs route definitions, not runtime config.
    deps = BackendDeps(
        config=PropsConfig(backend_url="http://localhost:0", agent_env={}),
        registry_proxy_config=RegistryProxyConfig(host="localhost", port=0),
        backend_url="http://localhost:0",
    )
    app = create_app(deps=deps, static_dir=None)
    schema = app.openapi()
    print(json.dumps(schema, indent=2))
