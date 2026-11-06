"""Real-registry lifecycle and ownership tests for the optional Props blob cache."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, closing
from urllib.parse import urljoin
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from testcontainers.core import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from devinfra.oci.oci import load_oci_image
from props.testing.fixtures.registry_blob_cache import (
    RegistryBlobCacheLeaseBusyError,
    RegistryBlobCacheOwnershipError,
    _registry_blob_cache_lease,
    cached_registry_container,
    delete_owned_registry_blob_cache_volumes,
)
from third_party.containers import registry_3, ryuk

_REPOSITORIES = ("critic",)
_OWNER_LABEL = "ducktape.props.registry-cache.owner"
_NAMESPACE_LABEL = "ducktape.props.registry-cache.namespace"


def _new_registry(image_ref: str) -> DockerContainer:
    return (
        DockerContainer(image_ref)
        .with_exposed_ports(5000)
        .with_env("REGISTRY_HTTP_RELATIVEURLS", "true")
        .waiting_for(LogMessageWaitStrategy("listening on"))
    )


def _manifest(config: bytes) -> tuple[str, bytes]:
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest, "size": len(config)},
            "layers": [],
        },
        separators=(",", ":"),
    ).encode()
    return digest, manifest


def _push_config(base_url: str, config: bytes, digest: str) -> None:
    location = _start_upload(base_url).copy_add_param("digest", digest)
    response = httpx.put(location, headers={"Content-Type": "application/octet-stream"}, content=config, timeout=10.0)
    assert response.status_code == 201, response.text


def _start_upload(base_url: str) -> httpx.URL:
    response = httpx.post(f"{base_url}/v2/critic/blobs/uploads/", timeout=10.0)
    assert response.status_code == 202
    return httpx.URL(urljoin(base_url, response.headers["Location"]))


def _put_manifest(base_url: str, tag: str, manifest: bytes) -> httpx.Response:
    return httpx.put(
        f"{base_url}/v2/critic/manifests/{tag}",
        headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
        content=manifest,
        timeout=10.0,
    )


def test_registry_keeps_only_blobs_and_layer_links_across_container_removal() -> None:
    namespace = f"test-{uuid4().hex}"
    image_ref = load_oci_image(registry_3.LAYOUT_RLOCATION)
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    controller = DockerContainer(image_ref)
    with ExitStack() as cleanup:
        docker_client = controller.get_docker_client().client
        cleanup.enter_context(closing(docker_client))
        cleanup.callback(delete_owned_registry_blob_cache_volumes, controller, namespace=namespace)
        foreign_name = f"props-registry-foreign-{namespace}"
        foreign_volume = docker_client.volumes.create(
            name=foreign_name, labels={_OWNER_LABEL: "another-owner", _NAMESPACE_LABEL: namespace}
        )
        cleanup.callback(foreign_volume.remove)

        config = json.dumps(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {},
                "rootfs": {"type": "layers", "diff_ids": []},
                "history": [],
            },
            separators=(",", ":"),
        ).encode()
        blob_digest, manifest = _manifest(config)
        manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()

        first = _new_registry(image_ref)
        with cached_registry_container(
            first, image_ref=image_ref, repositories=_REPOSITORIES, namespace=namespace
        ) as first_receipt:
            base_url = f"http://localhost:{first.get_exposed_port(5000)}"
            _push_config(base_url, config, blob_digest)
            assert _put_manifest(base_url, "first-tag", manifest).status_code == 201
            unfinished_upload = _start_upload(base_url).copy_add_param("digest", blob_digest)

        for name in first_receipt.volume_names:
            assert docker_client.volumes.get(name).name == name

        second = _new_registry(image_ref)
        with cached_registry_container(
            second, image_ref=image_ref, repositories=_REPOSITORIES, namespace=namespace
        ) as second_receipt:
            assert second_receipt.container_id != first_receipt.container_id
            assert second_receipt.volume_names == first_receipt.volume_names
            assert len(second_receipt.volume_names) == 1
            base_url = f"http://localhost:{second.get_exposed_port(5000)}"
            assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 200
            assert httpx.head(f"{base_url}/v2/critic/manifests/first-tag", timeout=10.0).status_code == 404
            assert httpx.head(f"{base_url}/v2/critic/manifests/{manifest_digest}", timeout=10.0).status_code == 404
            stale_upload = httpx.put(
                unfinished_upload.copy_with(host="localhost", port=int(second.get_exposed_port(5000))),
                headers={"Content-Type": "application/octet-stream"},
                content=config,
                timeout=10.0,
            )
            assert stale_upload.status_code == 404, stale_upload.text
            assert _put_manifest(base_url, "second-tag", manifest).status_code == 201

        deleted = delete_owned_registry_blob_cache_volumes(controller, namespace=namespace)
        assert set(deleted) == set(first_receipt.volume_names)
        assert docker_client.volumes.get(foreign_name).name == foreign_name

        wrong_owner = docker_client.volumes.create(
            name=first_receipt.volume_names[0], labels={_OWNER_LABEL: "another-owner", _NAMESPACE_LABEL: namespace}
        )
        cleanup.callback(wrong_owner.remove)
        attempt = _new_registry(image_ref)
        with (
            closing(attempt.get_docker_client().client),
            pytest.raises(RegistryBlobCacheOwnershipError),
            cached_registry_container(attempt, image_ref=image_ref, repositories=_REPOSITORIES, namespace=namespace),
        ):
            pytest.fail("cache with wrong owner labels started")

        assert docker_client.volumes.get(first_receipt.volume_names[0]).attrs["Labels"][_OWNER_LABEL] == "another-owner"
        assert delete_owned_registry_blob_cache_volumes(controller, namespace=namespace) == ()


def test_registry_cache_lease_fails_without_waiting_for_another_owner() -> None:
    with _registry_blob_cache_lease(), pytest.raises(RegistryBlobCacheLeaseBusyError), _registry_blob_cache_lease():
        pytest.fail("a second registry cache lifecycle acquired the lease")


if __name__ == "__main__":
    pytest_bazel.main()
