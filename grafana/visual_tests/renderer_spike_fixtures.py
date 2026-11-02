"""Resources shared by the upstream image-renderer experiments."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import urlsplit

import docker
import httpx
import pytest
from docker.models.containers import Container
from docker.models.networks import Network

from grafana.visual_tests.visual_harness import record_stage
from third_party.containers import grafana_image_renderer_spike
from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer


def load_images(*images: OciImage) -> None:
    """Prepare the isolated services concurrently before their fixtures start."""
    with ThreadPoolExecutor(max_workers=len(images)) as executor:
        list(executor.map(load_oci_image, images))


@pytest.fixture
def docker_client() -> Iterator[docker.DockerClient]:
    with closing(docker.from_env()) as client:
        yield client


@pytest.fixture
def grafana_network(docker_client: docker.DockerClient, grafana_url: str) -> Network:
    grafana_ip = urlsplit(grafana_url).hostname
    grafana = next(
        container
        for container in docker_client.containers.list()
        if any(n.get("IPAddress") == grafana_ip for n in container.attrs["NetworkSettings"]["Networks"].values())
    )
    return docker_client.networks.get(next(iter(grafana.attrs["NetworkSettings"]["Networks"])))


@pytest.fixture
def renderer_container(grafana_network: Network) -> Iterator[Container]:
    record_stage("starting upstream image renderer")
    container = (
        LoggedContainer(grafana_image_renderer_spike.IMAGE.tag, test_name="image-renderer")
        .with_kwargs(network=grafana_network.name)
        .with_envs(AUTH_TOKEN="spike", BROWSER_FLAGS="--no-sandbox")
    )
    with container:
        yield container.get_wrapped_container()


@pytest.fixture
def renderer_url(renderer_container: Container, grafana_network: Network) -> str:
    renderer_container.reload()
    address = renderer_container.attrs["NetworkSettings"]["Networks"][grafana_network.name]["IPAddress"]
    base = f"http://{address}:8081"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=0.2, trust_env=False).status_code == 200:
                return base
        except httpx.HTTPError:
            time.sleep(0.1)
    raise AssertionError("image renderer health endpoint did not respond")
