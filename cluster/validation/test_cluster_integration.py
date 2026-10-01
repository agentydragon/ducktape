"""Integration tests: validate the real manifest trees (cluster/k8s, cluster/generated) via pure analysis.

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
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.manifest_roots import PARKED_ROOT
from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_egress_bindings_resolve_policies,
    check_external_credential_ownership,
    check_forgejo_image_namespace_reflection,
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    check_sops_decryption_blocks,
    find_orphaned_files,
)
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux import parse_flux_kustomizations
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.image_automation import (
    check_image_automation_webhook,
    check_image_policy_markers,
    check_no_flow_mappings_where_flux_writes,
)
from cluster.validation.kustomize import KustomizeBuildResult, run_kustomize_build


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

    async def _build_all() -> list[KustomizeBuildResult]:
        return list(await asyncio.gather(*[run_kustomize_build(k) for k in kusts]))

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


def test_loki_proxy_static_allowlist_covers_agent_readable_log_namespaces(
    cluster: ParsedCluster, k8s_dir: Path, generated_dir: Path
) -> None:
    """A Namespace opt-in for Kubernetes pod logs must also permit its Loki logs.

    A bearer-token request is authorized by the same RBAC the label generates,
    but anonymous (token-less) callers are judged by this static allowlist; this
    CI contract makes the GitOps-owned logs label the review point for both.
    """
    deployment = one(
        obj
        for obj in yaml.safe_load_all((generated_dir / "agents/loki-read-proxy/loki-read-proxy.k8s.yaml").read_text())
        if obj["kind"] == "Deployment"
    )
    container = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "proxy")
    env = {entry["name"]: entry["value"] for entry in container["env"]}
    loki_allowlist = frozenset(namespace for namespace in env["NAMESPACE_ALLOWLIST"].split(",") if namespace)
    log_label = "rbac.ducktape.io/agent-readable-logs"
    labeled_namespaces = {
        resource.name
        for build in cluster.build_results
        for resource in build.resources
        if resource.kind == "Namespace" and resource.metadata.labels.get(log_label) == "true"
    }

    # flux-system applies this label through its bootstrap overlay rather than
    # a literal Namespace manifest, so retain the explicit assertion here.
    flux_system_kustomization = (k8s_dir / "flux/flux-system/kustomization.yaml").read_text()
    assert "path: /metadata/labels/rbac.ducktape.io~1agent-readable-logs" in flux_system_kustomization
    labeled_namespaces.add("flux-system")

    missing = sorted(labeled_namespaces - loki_allowlist)
    assert not missing, f"agent-readable log namespaces missing from Loki proxy allowlist: {missing}"


def test_agentplane_external_delegation_has_independent_flux_ownership(k8s_dir: Path, generated_dir: Path) -> None:
    """The managed-binding delegation has one target-owned Flux Kustomization."""
    name = "agentplane-staging-external-bindings"
    target = "haku-sandbox"
    app_docs = list(yaml.safe_load_all((k8s_dir / "agentplane-staging/agentplane-staging.k8s.yaml").read_text()))
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == target
        and doc["metadata"]["name"] in {"agentplane-staging-managed-bindings", name}
        for doc in app_docs
    )

    path = generated_dir / "agentplane/binding-delegation/agentplane-staging/haku-sandbox"
    objects = list(yaml.safe_load_all((path / "haku-sandbox.k8s.yaml").read_text()))
    role = one(doc for doc in objects if doc["kind"] == "Role")
    binding = one(doc for doc in objects if doc["kind"] == "RoleBinding")
    assert role["metadata"] == {"name": name, "namespace": target}
    assert role["rules"] == [
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["rolebindings"],
            "verbs": ["create", "get", "list", "delete"],
        },
        {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles"], "verbs": ["get"]},
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resourceNames": ["haku-sandbox-admin"],
            "resources": ["roles"],
            "verbs": ["bind"],
        },
    ]
    assert binding["metadata"] == {"name": name, "namespace": target}
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "agentplane-app", "namespace": "agentplane-staging"}
    ]
    assert yaml.safe_load((path / "kustomization.yaml").read_text())["resources"] == ["haku-sandbox.k8s.yaml"]

    flux_objects = yaml.safe_load_all((k8s_dir / "flux/kustomizations.k8s.yaml").read_text())
    flux = one(
        doc
        for doc in flux_objects
        if doc["kind"] == "Kustomization"
        and doc["metadata"]["name"] == "agentplane-staging-binding-delegation-haku-sandbox"
    )
    assert flux["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
    assert flux["spec"]["path"] == "./cluster/generated/agentplane/binding-delegation/agentplane-staging/haku-sandbox"
    assert flux["spec"]["dependsOn"] == [{"name": "haku-rbac", "namespace": "ducktape-flux"}]


def test_haku_service_read_delegation_is_scoped_to_owning_namespaces(k8s_dir: Path, generated_dir: Path) -> None:
    flux_objects = list(yaml.safe_load_all((k8s_dir / "flux/kustomizations.k8s.yaml").read_text()))
    for namespace, role_names, owner in (
        ("haku-console", ("agent-haku-console-metadata-reader",), "haku-console"),
        ("clickhouse", ("agent-clickhouse-diagnostics-reader",), "clickhouse"),
        ("ducktape-flux", ("ducktape-flux-reader",), None),
        (
            "public-coder-agent",
            (
                "agent-public-coder-extended-diagnostics-reader",
                "public-coder-agent-devbox-vmi-restart",
                "public-coder-agent-reader",
            ),
            "public-coder-agent-app",
        ),
    ):
        path = generated_dir / f"agentplane/binding-delegation/agentplane-staging/{namespace}"
        objects = list(yaml.safe_load_all((path / f"{namespace}.k8s.yaml").read_text()))
        role = one(doc for doc in objects if doc["kind"] == "Role")
        binding = one(doc for doc in objects if doc["kind"] == "RoleBinding")
        assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == namespace
        assert [rule for rule in role["rules"] if rule["verbs"] == ["bind"]] == [
            {
                "apiGroups": ["rbac.authorization.k8s.io"],
                "resourceNames": [role_name],
                "resources": ["roles"],
                "verbs": ["bind"],
            }
            for role_name in role_names
        ]
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "agentplane-app", "namespace": "agentplane-staging"}
        ]
        flux = one(
            doc
            for doc in flux_objects
            if doc["kind"] == "Kustomization"
            and doc["metadata"]["name"] == f"agentplane-staging-binding-delegation-{namespace}"
        )
        if owner is None:
            assert "dependsOn" not in flux["spec"]
        else:
            assert flux["spec"]["dependsOn"] == [{"name": owner, "namespace": "ducktape-flux"}]


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


def test_flux_kustomizations_chart_is_root_wired(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """The single generated Flux chart is reachable through both hand-written roots."""
    root_kust = next(
        k for path, k in cluster.kustomize_files.items() if path.resolve() == (k8s_dir / "kustomization.yaml").resolve()
    )
    flux_dir = (k8s_dir / "flux").resolve()
    assert flux_dir in {resource.resolve() for resource in root_kust.resolved_resources}

    flux_root = next(
        k
        for path, k in cluster.kustomize_files.items()
        if path.resolve() == (flux_dir / "kustomization.yaml").resolve()
    )
    chart_path = (flux_dir / "kustomizations.k8s.yaml").resolve()
    assert chart_path in {resource.resolve() for resource in flux_root.resolved_resources}


def test_flux_kustomizations_under_parked_path_are_annotated(k8s_dir: Path) -> None:
    """A Flux Kustomization whose path is under the parked tree must carry the parked annotation."""
    errors = []
    chart = k8s_dir / "flux/kustomizations.k8s.yaml"
    for name, spec in parse_flux_kustomizations(chart).items():
        if Path(spec.path.removeprefix("./")).is_relative_to(PARKED_ROOT) and not spec.parked:
            errors.append(
                f"{name} ({spec.path}): ducktape.org/parked annotation={spec.parked}, "
                f"but its source path is under {PARKED_ROOT}/"
            )
    assert not errors, "\n".join(errors)


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """A namespace with a goldilocks vpa-update-mode is not opted out of goldilocks."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_goldilocks_explicit_decision(cluster: ParsedCluster, repo_root: Path) -> None:
    """Every generated Namespace labels its goldilocks decision."""
    errors = check_goldilocks_explicit_decision(cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_egress_bindings_resolve_policies(cluster: ParsedCluster) -> None:
    """Every EgressBinding's policies are EgressPolicies rendered in its namespace."""
    errors = check_egress_bindings_resolve_policies(cluster)
    assert not errors, "\n".join(errors)


def test_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> None:
    """No Cilium policy rule with all rule sections empty (Cilium rejects it, silently unenforced)."""
    errors = check_cilium_policy_rules_nonempty(cluster)
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
