"""Contract for the staging Actions source-artifact pilot."""

import shutil
import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from cluster.validation.tool_resolve import resolve_tool
from util.bazel.runfiles import get_required_path


def test_actions_artifact_preserves_render_inputs(tmp_path: Path) -> None:
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent
    generator = yaml.safe_load((root / "agentplane-staging/actions-artifact/artifactgenerator.yaml").read_text())
    assert generator["spec"]["sources"] == [
        {"alias": "repo", "kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
    ]
    (artifact,) = generator["spec"]["artifacts"]
    assert artifact["name"] == "agentplane-staging-actions"
    assert "revision" not in artifact  # Content-derived, not the monorepo revision.
    assert artifact["originRevision"] == "@repo"
    consumer = yaml.safe_load((root / "agentplane-staging/actions/flux-kustomization.yaml").read_text())
    assert consumer["spec"]["sourceRef"] == {
        "kind": "ExternalArtifact",
        "name": artifact["name"],
        "namespace": generator["metadata"]["namespace"],
    }
    (operation,) = artifact["copy"]
    relative = "cluster/k8s/agentplane-staging/actions"
    assert consumer["spec"]["path"] == f"./{relative}"
    assert operation == {
        "from": f"@repo/{relative}/**",
        "to": f"@artifact/{relative}/",
        "exclude": ["flux-kustomization.yaml"],
    }
    source = root / "agentplane-staging/actions"
    packaged = tmp_path / relative
    shutil.copytree(source, packaged, ignore=shutil.ignore_patterns(*operation["exclude"]))
    assert not (packaged / "flux-kustomization.yaml").exists()
    kustomize = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")
    original = subprocess.run([kustomize, "build", str(source)], check=True, capture_output=True, text=True)
    rebuilt = subprocess.run([kustomize, "build", str(packaged)], check=True, capture_output=True, text=True)
    assert list(yaml.safe_load_all(rebuilt.stdout)) == list(yaml.safe_load_all(original.stdout))


def test_source_watcher_is_bootstrapped_with_shared_permissions() -> None:
    components = get_required_path("_main/cluster/k8s/flux-system/gotk-components.yaml")
    documents = list(yaml.safe_load_all(components.read_text()))
    deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "source-watcher")
    assert deployment["metadata"]["namespace"] == "flux-system"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/fluxcd/source-watcher:v2.1.1"
    assert any(
        d["kind"] == "CustomResourceDefinition" and d["spec"]["names"]["kind"] == "ArtifactGenerator" for d in documents
    )
    binding = next(
        d
        for d in documents
        if d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == "crd-controller-flux-system"
    )
    assert {"kind": "ServiceAccount", "name": "source-watcher", "namespace": "flux-system"} in binding["subjects"]


if __name__ == "__main__":
    pytest_bazel.main()
