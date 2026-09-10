"""Boot the published image with synthetic credentials and exercise both auth surfaces."""

import time
from pathlib import Path

import httpx
import pytest_bazel

from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer

IMAGE = OciImage("_main/third_party/cli_proxy_api_tests/image_layout.rloc", "cli-proxy-api:test")
CLIENT_KEY = "synthetic-model-client-key"
MANAGEMENT_KEY = "synthetic-management-key"


def test_image_serves_bundled_ui_and_preserves_key_authentication(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "host: 0.0.0.0\n"
        "port: 8317\n"
        "auth-dir: /tmp/test-auth\n"
        f"api-keys: [{CLIENT_KEY}]\n"
        "remote-management:\n"
        "  allow-remote: true\n"
    )
    with (
        LoggedContainer(
            load_oci_image(IMAGE),
            test_name="cli-proxy-api-boot",
            command=["-config", "/config/config.yaml"],
            volumes=[(str(tmp_path), "/config", "ro")],
        )
        .with_env("MANAGEMENT_PASSWORD", MANAGEMENT_KEY)
        .with_exposed_ports(8317) as container
    ):
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8317)}"
        with httpx.Client(base_url=base_url, timeout=2) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    response = client.get("/v0/management/session")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("CLIProxyAPI did not become ready; see undeclared container logs")
                time.sleep(0.1)

            assert response.json() == {"authenticated": False, "oidcEnabled": False}
            ui = client.get("/management.html")
            assert ui.status_code == 200
            assert "/v0/management/session" in ui.text
            assert "Sign in with SSO" in ui.text

            assert client.get("/v1/models").status_code == 401
            models = client.get("/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"})
            assert models.status_code == 200
            assert models.json()["object"] == "list"

            assert client.get("/v0/management/config").status_code == 401
            config_response = client.get("/v0/management/config", headers={"Authorization": f"Bearer {MANAGEMENT_KEY}"})
            assert config_response.status_code == 200
            assert config_response.json()["debug"] is False


if __name__ == "__main__":
    pytest_bazel.main()
