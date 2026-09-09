"""Terraform sources must resolve, contain the root, and satisfy controller policy."""

from pathlib import PurePosixPath

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path


@pytest.fixture(scope="module")
def resources() -> list[dict]:
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent
    return [
        document
        for path in root.rglob("*.yaml")
        # Authentik blueprints use custom YAML tags and are not Kubernetes resources.
        if "blueprints" not in path.parts
        for document in yaml.safe_load_all(path.read_text())
        if isinstance(document, dict) and document.get("kind") in {"Terraform", "GitRepository", "HelmRelease"}
    ]


def test_terraform_sources_resolve_and_include_root(resources: list[dict]) -> None:
    sources = {
        (resource["metadata"]["namespace"], resource["metadata"]["name"]): resource
        for resource in resources
        if resource["kind"] == "GitRepository"
    }
    terraforms = [resource for resource in resources if resource["kind"] == "Terraform"]
    assert terraforms
    for terraform in terraforms:
        ref = terraform["spec"]["sourceRef"]
        if ref["kind"] != "GitRepository":
            continue
        key = (ref.get("namespace", terraform["metadata"]["namespace"]), ref["name"])
        assert key in sources, f"{terraform['metadata']['name']}: missing GitRepository {key}"
        source = sources[key]
        if directories := source["spec"].get("sparseCheckout"):
            root = PurePosixPath(terraform["spec"]["path"])
            assert any(root.is_relative_to(directory) for directory in directories), (
                f"{terraform['metadata']['name']}: {root} is outside {key}'s sparse checkout"
            )


def test_controller_allows_cross_namespace_terraform_sources(resources: list[dict]) -> None:
    controller = next(
        resource
        for resource in resources
        if resource["kind"] == "HelmRelease" and resource["metadata"]["name"] == "tofu-controller"
    )
    for terraform in (resource for resource in resources if resource["kind"] == "Terraform"):
        namespace = terraform["metadata"]["namespace"]
        if terraform["spec"]["sourceRef"].get("namespace", namespace) != namespace:
            assert controller["spec"]["values"].get("allowCrossNamespaceRefs") is True


if __name__ == "__main__":
    pytest_bazel.main()
