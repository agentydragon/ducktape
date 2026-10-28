from __future__ import annotations

import time

import httpx
import pytest_bazel
from testcontainers.core.container import DockerContainer

from agentplane.app.frontend.serving.testing.image import IMAGE
from util.oci import load_oci_image


def _wait_for_nginx(base_url: str) -> httpx.Response:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=1)
            if response.status_code == 200:
                return response
        except httpx.TransportError:
            time.sleep(0.1)
    raise TimeoutError("Agentplane static nginx did not become ready within 15 seconds")


def test_static_image_serves_assets_without_revalidation() -> None:
    container = DockerContainer(load_oci_image(IMAGE)).with_exposed_ports(8080)
    container.with_tmpfs_mount("/tmp", "rw,noexec,nosuid,size=16m,uid=1000,gid=1000,mode=0700").with_kwargs(
        read_only=True
    )

    with container:
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        health = _wait_for_nginx(base_url)
        assert health.text == "ok\n"

        shell = httpx.get(f"{base_url}/", timeout=2)
        index = httpx.get(f"{base_url}/index.html", timeout=2)
        assert shell.status_code == index.status_code == 200
        assert shell.text == index.text
        assert 'href="/main.css"' in shell.text
        assert 'src="/main.js"' in shell.text
        assert shell.headers["cache-control"] == "no-store"
        assert "etag" not in shell.headers
        assert "last-modified" in shell.headers

        revalidated = httpx.get(
            f"{base_url}/index.html", headers={"If-Modified-Since": shell.headers["last-modified"]}, timeout=2
        )
        assert revalidated.status_code == 200
        assert revalidated.text == shell.text

        for path, media_type in (
            ("/main.js", "javascript"),
            ("/main.css", "text/css"),
            ("/favicon.svg", "image/svg+xml"),
            ("/sw.js", "javascript"),
        ):
            asset = httpx.get(f"{base_url}{path}", timeout=2)
            assert asset.status_code == 200, path
            assert asset.content
            assert media_type in asset.headers["content-type"], path
            assert asset.headers["cache-control"] == "no-store", path
            assert "etag" not in asset.headers, path

        missing = httpx.get(f"{base_url}/missing.js", timeout=2)
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"

        api_path = httpx.get(f"{base_url}/api/threads", timeout=2)
        assert api_path.status_code == 404
        assert api_path.text != shell.text
        assert api_path.headers["cache-control"] == "no-store"

        rendered = container.get_wrapped_container().exec_run(["nginx", "-T"])
        assert rendered.exit_code == 0, rendered.output
        nginx_config = rendered.output.decode()
        assert "listen 8080;" in nginx_config
        assert "worker_processes 1;" in nginx_config
        assert "proxy_pass" not in nginx_config


if __name__ == "__main__":
    pytest_bazel.main()
