from __future__ import annotations

import re
import time

import httpx
import pytest_bazel
from testcontainers.core.container import DockerContainer

from util.oci import OciImage, load_oci_image

_STATIC_IMAGE = OciImage("_main/haku/console/static_test_image.rloc", "haku-console-static:test")


def _wait_for_nginx(base_url: str) -> httpx.Response:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            response = httpx.get(base_url, timeout=1)
            if response.status_code == 200:
                return response
        except httpx.TransportError:
            time.sleep(0.1)
    raise TimeoutError("haku-console static nginx did not become ready within 15 seconds")


def test_static_image_cache_contract() -> None:
    container = (
        DockerContainer(load_oci_image(_STATIC_IMAGE))
        .with_env("HAKU_CONSOLE_HAKU_UI_URL", "https://haku-ui.test")
        .with_env("HAKU_CONSOLE_AUTH_ORIGIN", "https://auth.test")
        .with_exposed_ports(8081)
    )

    with container:
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8081)}"
        shell = _wait_for_nginx(base_url)
        assert shell.headers["cache-control"] == "no-store"
        assert "etag" not in shell.headers

        revalidated = httpx.get(base_url, headers={"If-Modified-Since": shell.headers["last-modified"]})
        assert revalidated.status_code == 200
        assert revalidated.headers["cache-control"] == "no-store"

        match = re.search(r'src="(?P<path>/assets/[^"]+\.js)"', shell.text)
        assert match is not None
        asset = httpx.get(f"{base_url}{match.group('path')}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

        missing = httpx.get(f"{base_url}/assets/not-current.js")
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"


if __name__ == "__main__":
    pytest_bazel.main()
