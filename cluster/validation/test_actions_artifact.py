"""Contracts for source-artifact migrations."""

import shutil
import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from cluster.validation.tool_resolve import resolve_tool
from util.bazel.runfiles import get_required_path


def test_artifact_generators_preserve_render_inputs(tmp_path: Path) -> None:
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent
    root_kustomization = yaml.safe_load((root / "kustomization.yaml").read_text())
    expected_source = {"alias": "repo", "kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
    kustomize = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")

    artifact_relative = "artifact-generators"
    generators = {
        document["metadata"]["name"]: document
        for document in yaml.safe_load_all((root / artifact_relative / "generators.yaml").read_text())
        if document and document.get("kind") == "ArtifactGenerator"
    }
    cases = (
        ("agentplane-staging-actions", "agentplane-staging/actions"),
        ("agentplane-staging-app", "agentplane-staging/app"),
        ("agentplane-staging-namespace", "agentplane-staging/namespace"),
        ("agentplane-testing-actions", "agentplane-testing/actions"),
        ("claude-rbac", "agents/agent-rbac-base"),
        ("agent-machine-access-tf", "agents/machine-access-tf"),
        ("authentik", "authentik/app"),
        ("sso-providers-tf", "authentik/sso-providers-tf"),
        ("cert-manager", "cert-manager/app"),
        ("cert-manager-environment", "cert-manager/environment"),
        ("cert-manager-issuer-config", "cert-manager/issuer-config"),
        ("cnpg", "cnpg"),
        ("external-secrets-config", "external-secrets/config"),
        ("external-secrets-operator", "external-secrets/operator"),
        ("forgejo", "forgejo/app"),
        ("forgejo-images", "forgejo-images"),
        ("gateway", "gateway"),
        ("kyverno", "kyverno/app"),
        ("litellm-keys-tf", "litellm/keys-tf"),
        ("local-path-provisioner", "local-path-provisioner"),
        ("monitoring-stack", "monitoring/stack"),
        ("reflector", "reflector"),
        ("seaweedfs-cluster", "seaweedfs/cluster"),
        ("seaweedfs-filer-db", "seaweedfs/db"),
        ("seaweedfs-namespace", "seaweedfs/namespace"),
        ("seaweedfs-operator", "seaweedfs/operator"),
        ("seaweedfs-secrets", "seaweedfs/secrets"),
        ("tofu-controller", "tofu-controller"),
        ("tofu-state-db", "tofu-state/db"),
        ("valkey", "valkey"),
        ("kyverno-policies", "kyverno/policies"),
        ("haku-state", "forgejo/haku-state"),
        ("agentplane-crds", "agentplane-crds"),
        ("monitoring-namespace", "monitoring/namespace"),
        ("haku-namespace", "haku/namespace"),
        ("langfuse-namespace", "langfuse/namespace"),
        ("volsync", "volsync"),
        ("authentik-namespace", "authentik/namespace"),
        ("cert-manager-trust", "cert-manager/trust"),
        ("agent-sandbox-controller", "agents/agent-sandbox/controller"),
    )

    assert set(generators) == {artifact_name for artifact_name, _ in cases}
    for artifact_name, source_relative in cases:
        relative = f"cluster/k8s/{source_relative}"
        generator = generators[artifact_name]
        assert generator["spec"]["sources"] == [expected_source]
        (artifact,) = generator["spec"]["artifacts"]
        assert artifact["name"] == artifact_name
        assert "revision" not in artifact  # Content-derived, not the monorepo revision.
        assert artifact["originRevision"] == "@repo"
        consumer = yaml.safe_load((root / f"{source_relative}/flux-kustomization.yaml").read_text())
        assert consumer["spec"]["sourceRef"] == {
            "kind": "ExternalArtifact",
            "name": artifact["name"],
            "namespace": generator["metadata"]["namespace"],
        }
        assert consumer["spec"]["path"] == f"./{relative}"
        primary_operation = next(
            operation for operation in artifact["copy"] if operation["to"] == f"@artifact/{relative}/"
        )
        assert primary_operation == {
            "from": f"@repo/{relative}/**",
            "to": f"@artifact/{relative}/",
            "exclude": ["flux-kustomization.yaml"],
        }
        assert f"{artifact_relative}/flux-kustomization.yaml" in root_kustomization["resources"]
        source = root / source_relative
        packaged = tmp_path / relative
        for operation in artifact["copy"]:
            operation_source_relative = operation["from"].removeprefix("@repo/").removesuffix("/**")
            operation_source = root / operation_source_relative.removeprefix("cluster/k8s/")
            operation_target = tmp_path / operation["to"].removeprefix("@artifact/")
            shutil.copytree(
                operation_source, operation_target, ignore=shutil.ignore_patterns(*operation.get("exclude", []))
            )
        assert not (packaged / "flux-kustomization.yaml").exists()
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
