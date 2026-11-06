"""Real-registry lifecycle and ownership tests for the optional Props blob cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import ExitStack, closing
from urllib.parse import urljoin
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from docker import DockerClient, from_env
from filelock import Timeout
from testcontainers.core import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from devinfra.oci.oci import load_oci_image
from props.testing.fixtures.registry_blob_cache import (
    RegistryBlobCacheOwnershipError,
    RegistryBlobCacheRecipe,
    _registry_blob_cache_lock,
    cached_registry_container,
)
from third_party.containers import registry_3, ryuk

_RECIPE = RegistryBlobCacheRecipe(key="registry-cache-test-v1", max_bytes=1024 * 1024)
_OWNER_LABEL = "ducktape.props.registry-cache.owner"
_NAMESPACE_LABEL = "ducktape.props.registry-cache.namespace"
_RECIPE_LABEL = "ducktape.props.registry-cache.recipe"
_OWNER = "props.testing.fixtures.registry_blob_cache.v1"


@pytest.fixture(scope="session")
def image_ref() -> str:
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    return load_oci_image(registry_3.LAYOUT_RLOCATION)


@pytest.fixture
def docker_client() -> Iterator[DockerClient]:
    with closing(from_env()) as client:
        yield client


@pytest.fixture
def namespace(docker_client: DockerClient) -> Iterator[str]:
    namespace = f"test-{uuid4().hex}"
    yield namespace
    with _registry_blob_cache_lock():
        for volume in docker_client.volumes.list(filters={"label": f"{_NAMESPACE_LABEL}={namespace}"}):
            labels = volume.attrs.get("Labels") or {}
            if (
                labels.get(_NAMESPACE_LABEL) == namespace
                and labels.get(_OWNER_LABEL) == _OWNER
                and labels.get(_RECIPE_LABEL)
            ):
                volume.remove()


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


def _head_manifest(base_url: str, reference: str) -> httpx.Response:
    return httpx.head(
        f"{base_url}/v2/critic/manifests/{reference}",
        headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
        timeout=10.0,
    )


def test_registry_keeps_only_blobs_and_layer_links_across_container_removal(
    image_ref: str, namespace: str, docker_client: DockerClient
) -> None:
    with ExitStack() as cleanup:
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
            first, image_ref=image_ref, recipe=_RECIPE, namespace=namespace
        ) as first_receipt:
            base_url = f"http://localhost:{first.get_exposed_port(5000)}"
            _push_config(base_url, config, blob_digest)
            assert _put_manifest(base_url, "first-tag", manifest).status_code == 201
            assert _head_manifest(base_url, "first-tag").status_code == 200
            # Digest-only manifests are invisible to tags/list cleanup.
            untagged_manifest = json.dumps({**json.loads(manifest), "annotations": {"test": "untagged"}}).encode()
            untagged_digest = "sha256:" + hashlib.sha256(untagged_manifest).hexdigest()
            assert _put_manifest(base_url, untagged_digest, untagged_manifest).status_code == 201
            assert _head_manifest(base_url, untagged_digest).status_code == 200
            unfinished_upload = _start_upload(base_url).copy_add_param("digest", blob_digest)

        assert docker_client.volumes.get(first_receipt.volume_name).name == first_receipt.volume_name

        second = _new_registry(image_ref)
        with cached_registry_container(
            second, image_ref=image_ref, recipe=_RECIPE, namespace=namespace
        ) as second_receipt:
            assert second_receipt.container_id != first_receipt.container_id
            assert second_receipt.volume_name == first_receipt.volume_name
            base_url = f"http://localhost:{second.get_exposed_port(5000)}"
            assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 200
            assert _head_manifest(base_url, "first-tag").status_code == 404
            assert _head_manifest(base_url, manifest_digest).status_code == 404
            assert _head_manifest(base_url, untagged_digest).status_code == 404
            stale_upload = httpx.put(
                unfinished_upload.copy_with(host="localhost", port=int(second.get_exposed_port(5000))),
                headers={"Content-Type": "application/octet-stream"},
                content=config,
                timeout=10.0,
            )
            assert stale_upload.status_code == 404, stale_upload.text
            assert _put_manifest(base_url, "second-tag", manifest).status_code == 201

        incompatible = _new_registry(image_ref)
        with cached_registry_container(
            incompatible,
            image_ref=image_ref,
            recipe=_RECIPE.model_copy(update={"key": _RECIPE.key + "-changed"}),
            namespace=namespace,
        ) as incompatible_receipt:
            assert incompatible_receipt.volume_name != first_receipt.volume_name
            base_url = f"http://localhost:{incompatible.get_exposed_port(5000)}"
            assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 404

        with ExitStack() as attached_cleanup:
            attached = docker_client.containers.create(
                image_ref, volumes={first_receipt.volume_name: {"bind": "/var/lib/registry", "mode": "rw"}}
            )
            attached_cleanup.callback(attached.remove, force=True)
            conflict = _new_registry(image_ref)
            with (
                closing(conflict.get_docker_client().client),
                pytest.raises(RegistryBlobCacheOwnershipError, match="already attached"),
                cached_registry_container(conflict, image_ref=image_ref, recipe=_RECIPE, namespace=namespace),
            ):
                pytest.fail("cache mounted by another container was reset")
            attached.reload()
            assert attached.status == "created"

        docker_client.volumes.get(first_receipt.volume_name).remove()
        assert docker_client.volumes.get(foreign_name).name == foreign_name

        wrong_owner = docker_client.volumes.create(
            name=first_receipt.volume_name, labels={_OWNER_LABEL: "another-owner", _NAMESPACE_LABEL: namespace}
        )
        cleanup.callback(wrong_owner.remove)
        attempt = _new_registry(image_ref)
        with (
            closing(attempt.get_docker_client().client),
            pytest.raises(RegistryBlobCacheOwnershipError),
            cached_registry_container(attempt, image_ref=image_ref, recipe=_RECIPE, namespace=namespace),
        ):
            pytest.fail("cache with wrong owner labels started")

        assert docker_client.volumes.get(first_receipt.volume_name).attrs["Labels"][_OWNER_LABEL] == "another-owner"


@pytest.mark.parametrize("boundary", ["attach", "release"])
def test_registry_cache_discards_oversized_blobs_at_the_safe_boundary(
    boundary: str, image_ref: str, namespace: str
) -> None:
    recipe = RegistryBlobCacheRecipe(key="registry-cache-budget-test-v1", max_bytes=64 * 1024)
    seed = _new_registry(image_ref)
    with cached_registry_container(seed, image_ref=image_ref, recipe=recipe, namespace=namespace) as receipt:
        volume_name = receipt.volume_name

    # An ordinary container models leftover storage from an interrupted owner.
    # Inspect release through an ordinary container too, so attach cleanup
    # cannot hide a missing release-time bound.
    writer = _new_registry(image_ref)
    if boundary == "attach":
        writer.with_volume_mapping(volume_name, "/var/lib/registry", mode="rw")
    with (
        writer
        if boundary == "attach"
        else cached_registry_container(writer, image_ref=image_ref, recipe=recipe, namespace=namespace)
    ):
        base_url = f"http://localhost:{writer.get_exposed_port(5000)}"
        config = json.dumps({"padding": "x" * (256 * 1024)}).encode()
        blob_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        _push_config(base_url, config, blob_digest)
        assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 200

    reader = _new_registry(image_ref)
    if boundary == "release":
        reader.with_volume_mapping(volume_name, "/var/lib/registry", mode="rw")
    with (
        reader
        if boundary == "release"
        else cached_registry_container(reader, image_ref=image_ref, recipe=recipe, namespace=namespace)
    ):
        base_url = f"http://localhost:{reader.get_exposed_port(5000)}"
        assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 404
        small_config = b"{}"
        small_digest = "sha256:" + hashlib.sha256(small_config).hexdigest()
        _push_config(base_url, small_config, small_digest)
        assert httpx.head(f"{base_url}/v2/critic/blobs/{small_digest}", timeout=10.0).status_code == 200


def test_failed_registry_stop_preserves_attached_storage(
    monkeypatch: pytest.MonkeyPatch, image_ref: str, namespace: str
) -> None:
    recipe = RegistryBlobCacheRecipe(key="registry-cache-stop-test-v1", max_bytes=64 * 1024)
    registry = _new_registry(image_ref)

    def fail_to_stop() -> None:
        raise RuntimeError("simulated Docker stop failure")

    with ExitStack() as cleanup:
        cleanup.enter_context(closing(registry.get_docker_client().client))
        cleanup.callback(registry.stop)
        with monkeypatch.context() as patcher, ExitStack() as lifetime:
            patcher.setattr(registry, "stop", fail_to_stop)
            lifetime.enter_context(
                cached_registry_container(registry, image_ref=image_ref, recipe=recipe, namespace=namespace)
            )
            base_url = f"http://localhost:{registry.get_exposed_port(5000)}"
            config = json.dumps({"padding": "x" * (256 * 1024)}).encode()
            blob_digest, manifest = _manifest(config)
            _push_config(base_url, config, blob_digest)
            assert _put_manifest(base_url, "preserve-me", manifest).status_code == 201
            with pytest.raises(RegistryBlobCacheOwnershipError):
                lifetime.close()

        assert httpx.head(f"{base_url}/v2/critic/blobs/{blob_digest}", timeout=10.0).status_code == 200
        assert _head_manifest(base_url, "preserve-me").status_code == 200


def test_registry_cache_lease_fails_without_waiting_for_another_owner() -> None:
    with _registry_blob_cache_lock(), pytest.raises(Timeout), _registry_blob_cache_lock():
        pytest.fail("a second registry cache lifecycle acquired the lease")


if __name__ == "__main__":
    pytest_bazel.main()
