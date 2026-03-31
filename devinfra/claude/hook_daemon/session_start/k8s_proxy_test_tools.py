"""Dispatcher for combined k8s proxy test container image.

Routes to mock_k8s_server or k8s_proxy_test_client based on the
K8S_PROXY_TEST_ROLE environment variable. Exists because py_image_layer
can only have one main= per binary, but we need both scripts in one
image to avoid duplicate ~112 MB interpreter layers.
"""

import importlib
import os
import sys

_ROLES = {
    "mock_k8s_server": "devinfra.claude.hook_daemon.session_start.mock_k8s_server",
    "k8s_proxy_test_client": "devinfra.claude.hook_daemon.session_start.k8s_proxy_test_client",
}


def main() -> None:
    role = os.environ.get("K8S_PROXY_TEST_ROLE", "mock_k8s_server")
    module_path = _ROLES.get(role)
    if not module_path:
        print(f"Unknown role: {role!r}. Valid: {sorted(_ROLES)}", file=sys.stderr)
        sys.exit(1)
    importlib.import_module(module_path).main()


if __name__ == "__main__":
    main()
