"""Integration tests: validate the real manifest trees via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources).
All kustomizations are built with kustomize to validate they render correctly
and to provide build results for resource-level checks. Direct-file contracts
live in narrower sibling targets and do not pay this full-cluster setup cost.

These Bazel tests are the single source of truth for cluster validation; the
former `cluster-validate` pre-commit hook was removed in favor of running them
(and the sibling `test_*.py` targets) in CI.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_external_credential_ownership,
    check_forgejo_image_namespace_reflection,
    check_goldilocks_namespace_labels,
    check_sops_decryption_blocks,
    find_orphaned_files,
)
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux import EXTERNAL_ARTIFACT_KIND
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.image_automation import (
    check_image_automation_webhook,
    check_image_policy_markers,
    check_no_flow_mappings_where_flux_writes,
)
from cluster.validation.k8s import ArtifactGeneratorResource, K8sResource
from cluster.validation.kustomize import (
    KustomizeBuildResult,
    KustomizeFile,
    flux_generated_kustomization,
    parse_kustomize_file,
    run_kustomize_build,
    stage_artifact_copies,
)


def _local_flux_kust_names(parsed: ParsedCluster, repo_root: Path) -> set[str]:
    """Active flux kustomization names whose spec.path points into a local manifest root."""
    return {name for name, spec in parsed.active_flux_kustomizations.items() if spec.local_dir(repo_root)}


@pytest.fixture(scope="session")
def cluster(repo_root: Path) -> ParsedCluster:
    """Parse cluster and build flux-referenced kustomizations (hard failure on any build error)."""
    parsed = parse_cluster(repo_root)

    # Build all local flux-referenced kustomizations (including suspended — kustomize
    # build should still succeed). Only validation checks filter suspended.
    local_dirs = {d for spec in parsed.flux_kustomizations.values() if (d := spec.local_dir(repo_root))}
    kusts = [k for path, k in parsed.kustomize_files.items() if path.parent.resolve() in local_dirs]

    artifact_specs: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for manifest_path, resources in parsed.source_resources.items():
        generator_resources = [resource for resource in resources if isinstance(resource, ArtifactGeneratorResource)]
        if not generator_resources:
            continue
        for document in yaml.safe_load_all(manifest_path.read_text()):
            if not isinstance(document, dict) or document.get("kind") != "ArtifactGenerator":
                continue
            namespace = document.get("metadata", {}).get("namespace", "")
            for artifact in document.get("spec", {}).get("artifacts", []):
                artifact_specs[(namespace, artifact["name"])] = [
                    (copy["from"], copy["to"]) for copy in artifact.get("copy", [])
                ]
    specs_by_dir = {
        directory.resolve(): spec
        for spec in parsed.flux_kustomizations.values()
        if (directory := spec.local_dir(repo_root)) is not None
    }

    def needs_artifact_staging(copies: list[tuple[str, str]]) -> bool:
        # Directory copies already appear at their artifact paths in the source checkout.
        # Exact-file copies and remapped directories need a temporary materialized artifact.
        for source_uri, destination_uri in copies:
            if not source_uri.startswith("@repo/") or not destination_uri.startswith("@artifact/"):
                continue
            source = source_uri.removeprefix("@repo/")
            if source == "**":
                source = ""
            elif source.endswith("/**"):
                source = source.removesuffix("/**").rstrip("/")
            destination = destination_uri.removeprefix("@artifact/").rstrip("/")
            if source != destination:
                return True
        return False

    staged_artifacts: dict[tuple[str, str], Path] = {}
    with ExitStack() as staging_dirs:
        for key, copies in artifact_specs.items():
            if not needs_artifact_staging(copies):
                continue
            staging_root = Path(staging_dirs.enter_context(tempfile.TemporaryDirectory(prefix="flux-artifact-")))
            try:
                stage_artifact_copies(repo_root, staging_root, copies)
            except (ValueError, FileNotFoundError) as error:
                raise ValueError(f"failed to stage ExternalArtifact {key}: {error}") from error
            staged_artifacts[key] = staging_root

        async def _build_all() -> list[KustomizeBuildResult]:
            async def _build(kust: KustomizeFile) -> KustomizeBuildResult:
                spec = specs_by_dir[kust.path.parent.resolve()]
                source_ref = spec.source_ref
                if source_ref is None or source_ref.kind != EXTERNAL_ARTIFACT_KIND:
                    return await run_kustomize_build(kust)

                artifact_namespace = source_ref.namespace or spec.namespace
                staged_root = staged_artifacts.get((artifact_namespace, source_ref.name))
                if staged_root is None:
                    return await run_kustomize_build(kust)

                staged_path = staged_root / kust.path.relative_to(repo_root)
                if not staged_path.is_file():
                    raise FileNotFoundError(
                        f"ExternalArtifact {artifact_namespace}/{source_ref.name} does not contain "
                        f"{staged_path.relative_to(staged_root)}"
                    )
                staged_result = await run_kustomize_build(KustomizeFile(path=staged_path))
                return staged_result.model_copy(update={"kustomization_path": kust.path})

            return await asyncio.gather(*[_build(kust) for kust in kusts])

        parsed.build_results = asyncio.run(_build_all())
    return parsed


def test_all_local_flux_kustomizations_have_build_results(cluster: ParsedCluster, repo_root: Path) -> None:
    """Every flux kustomization pointing to a local path must have a build result."""
    covered = set(cluster.flux_kust_resources(repo_root))
    expected = _local_flux_kust_names(cluster, repo_root)
    missing = sorted(expected - covered)
    assert not missing, "Flux kustomizations with no build result:\n" + "\n".join(f"  {m}" for m in missing)


def test_no_dependency_errors(cluster: ParsedCluster, repo_root: Path) -> None:
    """Operator prerequisites are dependencies; every sourceRef resolves."""
    errors = validate_dependencies(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster, repo_root: Path) -> None:
    errors = check_controller_health_checks(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_external_credential_ownership(cluster: ParsedCluster, repo_root: Path) -> None:
    """Only the shared store reads external-creds, admitting exactly the source-approved namespaces."""
    errors = check_external_credential_ownership(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_forgejo_image_namespaces_are_reflected(cluster: ParsedCluster) -> None:
    """Every rendered workload using a Forgejo image can receive its pull secret."""
    errors = check_forgejo_image_namespace_reflection(cluster)
    assert not errors, "\n".join(errors)


def test_image_automation_webhook_consistency(cluster: ParsedCluster) -> None:
    """Every rendered GHCR ImageRepository is in the webhook Receiver, and the Receiver names no missing one.

    Runs against the real built cluster (not synthetic fixtures), so it also guards the
    check against crashing on the actual manifest set — the gap that hid the earlier
    raw-YAML-walking bug.
    """
    errors = check_image_automation_webhook(cluster)
    assert not errors, "\n".join(errors)


def test_image_policy_markers_resolve(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every `$imagepolicy` marker names a defined ImagePolicy, so no image silently stops rolling."""
    errors = check_image_policy_markers(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_agent_binding_delegation_dependencies_follow_namespace_owners(
    cluster: ParsedCluster, staging_policy_sets: dict[str, list[dict]], k8s_dir: Path, repo_root: Path
) -> None:
    """Delegated binding applies follow the namespace owner that creates their targets."""
    retained_resources = cluster.flux_kust_resources(repo_root, include_suspended=True)
    namespace_owners: dict[str, set[str]] = {}
    for owner, resources in retained_resources.items():
        for resource in resources:
            if resource.kind != "Namespace":
                continue
            namespace_owners.setdefault(resource.name, set()).add(owner)

    policy_set_namespaces = {
        namespace
        for rules in staging_policy_sets.values()
        for rule in rules
        if rule["type"] == "kubernetes_role_binding"
        for namespace in rule["namespaces"]
    }
    # Delegated: every PolicySet RoleBinding scope, the retained cleanup scopes, and the legacy
    # static-only litellm role, whose binding remains outside PolicySets.
    expected_external_scopes = (policy_set_namespaces - {"agentplane-staging"}) | {
        "ducktape-flux",
        "haku-console",
        "haku-sandbox",
        "litellm",
    }

    flux_objects = list(yaml.safe_load_all((k8s_dir / "flux/kustomizations.k8s.yaml").read_text()))
    delegation_name = "agentplane-staging-binding-delegation-"
    flux_by_name = {
        doc["metadata"]["name"]: doc
        for doc in flux_objects
        if doc["kind"] == "Kustomization" and doc["metadata"]["name"].startswith(delegation_name)
    }
    expected_flux_names = {f"{delegation_name}{namespace}" for namespace in expected_external_scopes}
    assert set(flux_by_name) == expected_flux_names

    namespace_owners["haku-sandbox"] = {"haku-rbac"}
    namespace_owners["flux-system"] = set()
    namespace_owners["ducktape-flux"] = set()
    delegation_owner_overrides = {"clickhouse": "clickhouse"}
    for namespace in expected_external_scopes:
        owners = namespace_owners[namespace]
        is_bootstrap_root = namespace in {"flux-system", "ducktape-flux"}
        expected_dependency = (
            None
            if is_bootstrap_root
            else [delegation_owner_overrides.get(namespace, owner) for owner in sorted(owners)]
        )
        assert is_bootstrap_root or len(owners) == 1, (namespace, owners)

        flux = flux_by_name[f"{delegation_name}{namespace}"]
        assert flux["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
        assert flux["spec"]["path"] == (
            f"./cluster/generated/agentplane/binding-delegation/agentplane-staging/{namespace}"
        )
        if expected_dependency is None:
            assert "dependsOn" not in flux["spec"]
        else:
            assert flux["spec"]["dependsOn"] == [{"name": expected_dependency[0], "namespace": "ducktape-flux"}]


def test_files_flux_rewrites_use_block_style(k8s_dir: Path) -> None:
    """A flow mapping in a file Flux rewrites fails prettier on every open PR at once.

    Flux re-serialises the whole document to set a tag, dropping prettier's inner spaces, and
    that lands on `devel` with `[skip ci]` — so the failure surfaces in unrelated PRs.
    """
    errors = check_no_flow_mappings_where_flux_writes(k8s_dir)
    assert not errors, "\n".join(errors)


def test_flux_bootstrap_sources_need_no_decrypted_auth(k8s_dir: Path) -> None:
    """Cold bootstrap sources must not depend on Flux-decrypted auth."""
    errors = check_flux_bootstrap_auth(k8s_dir)
    assert not errors, "\n".join(errors)


def test_sops_secrets_have_decryption_block(cluster: ParsedCluster, repo_root: Path) -> None:
    """Active flux kustomizations rendering a SOPS Secret must declare decryption.provider: sops."""
    errors = check_sops_decryption_blocks(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_retry_policy(cluster: ParsedCluster) -> None:
    check_retry_policy(cluster)


def test_no_orphaned_files(cluster: ParsedCluster, repo_root: Path) -> None:
    """All active YAML files must be referenced by a kustomization.yaml."""
    errors = find_orphaned_files(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """A namespace with a goldilocks vpa-update-mode is not opted out of goldilocks."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> None:
    """No Cilium policy rule with all rule sections empty (Cilium rejects it, silently unenforced)."""
    errors = check_cilium_policy_rules_nonempty(cluster)
    assert not errors, "\n".join(errors)


@pytest.fixture(scope="module")
def bootstrap_resources(k8s_dir: Path) -> list[K8sResource]:
    """Bootstrap roots are not children of the generated Flux graph; build them rather than guessing their labels."""
    bootstrap_roots = (
        parse_kustomize_file(k8s_dir / "flux/flux-system/kustomization.yaml"),
        flux_generated_kustomization(k8s_dir / "flux/ducktape-flux"),
    )
    return [resource for root in bootstrap_roots for resource in asyncio.run(run_kustomize_build(root)).resources]


@pytest.fixture(scope="module")
def agent_permission_resources(
    cluster: ParsedCluster, bootstrap_resources: list[K8sResource], repo_root: Path
) -> list[K8sResource]:
    # Suspension stops reconciliation, not the grants already present in a retained namespace.
    resources = [
        resource
        for group in cluster.flux_kust_resources(repo_root, include_suspended=True).values()
        for resource in group
    ]
    resources.extend(bootstrap_resources)
    return resources


@pytest.fixture(scope="module")
def staging_manifest_docs(k8s_dir: Path) -> list[dict]:
    return list(yaml.safe_load_all((k8s_dir / "agentplane-staging/agentplane-staging.k8s.yaml").read_text()))


@pytest.fixture(scope="module")
def staging_policy_sets(staging_manifest_docs: list[dict]) -> dict[str, list[dict]]:
    return {
        doc["metadata"]["name"]: doc["spec"]["rules"] for doc in staging_manifest_docs if doc["kind"] == "PolicySet"
    }


def test_no_legacy_agent_diagnostic_rbac(agent_permission_resources: list[K8sResource]) -> None:
    """The delivered cluster has no implicit Kyverno-generated agent readers."""
    assert not any(
        resource.kind == "ClusterPolicy" and resource.name == "generate-agent-diagnostics-readers"
        for resource in agent_permission_resources
    )
    assert not any(
        resource.kind == "ClusterRole" and resource.name == "kyverno-background-controller-rolebindings"
        for resource in agent_permission_resources
    )
    assert not any(
        resource.kind == "RoleBinding" and resource.name in {"agent-readable-metadata", "agent-readable-logs"}
        for resource in agent_permission_resources
    )


if __name__ == "__main__":
    pytest_bazel.main()
