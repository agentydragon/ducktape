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
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.manifest_roots import PARKED_ROOT
from cluster.validation.agent_rbac import Permission, Rbac, uncovered
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
from cluster.validation.k8s import K8sResource, RbacRoleRef, RoleBindingResource
from cluster.validation.kustomize import (
    KustomizeBuildResult,
    flux_generated_kustomization,
    parse_kustomize_file,
    run_kustomize_build,
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


def test_plaid_mcp_is_not_gated_by_unrelated_authentik_terraform(cluster: ParsedCluster) -> None:
    """Auth service and secret operator are prerequisites; other SSO apps are not."""
    spec = cluster.flux_kustomizations["plaid-mcp"]
    assert {dependency.name for dependency in spec.depends_on} == {"cnpg", "external-secrets-operator", "authentik"}
    assert spec.wait


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
    cluster: ParsedCluster, bootstrap_resources: list[K8sResource], generated_dir: Path
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
    rendered = [resource for build in cluster.build_results for resource in build.resources] + bootstrap_resources
    labeled_namespaces = {
        resource.name
        for resource in rendered
        if resource.kind == "Namespace" and resource.metadata.labels.get(log_label) == "true"
    }

    missing = sorted(labeled_namespaces - loki_allowlist)
    assert not missing, f"agent-readable log namespaces missing from Loki proxy allowlist: {missing}"


@pytest.mark.parametrize("preset", ["public-coder", "finance-agent"])
def test_managed_agent_read_grants_cover_declarative_namespace_opt_ins(
    cluster: ParsedCluster,
    bootstrap_resources: list[K8sResource],
    k8s_dir: Path,
    repo_root: Path,
    generated_dir: Path,
    preset: str,
) -> None:
    """Managed agents get the same labeled namespace readers as the static identities.

    Only active Namespace manifests participate. Shared policy emits labels,
    static bindings, and catalog/default entries; namespace dependencies stay explicit.
    """
    metadata_label = "rbac.ducktape.io/agent-readable-metadata"
    logs_label = "rbac.ducktape.io/agent-readable-logs"
    expected: set[tuple[str, str]] = set()

    def expect_readers(namespace: K8sResource) -> None:
        labels = namespace.metadata.labels
        if labels.get(metadata_label) == "true" or labels.get(logs_label) == "true":
            expected.add((namespace.name, "agent-readable-namespace-metadata"))
        if labels.get(logs_label) == "true":
            expected.add((namespace.name, "agent-readable-namespace-logs"))

    # Props is separately sourced and absent live; parked workloads are suspended.
    # Match static Haku's live scope through active, local Flux builds only.
    active_resources = cluster.flux_kust_resources(repo_root)
    namespace_owners: dict[str, set[str]] = {}
    for owner, resources in active_resources.items():
        for resource in resources:
            if resource.kind != "Namespace":
                continue
            namespace_owners.setdefault(resource.name, set()).add(owner)
            expect_readers(resource)
    for resource in bootstrap_resources:
        if resource.kind == "Namespace":
            expect_readers(resource)

    staging_docs = list(yaml.safe_load_all((k8s_dir / "agentplane-staging/agentplane-staging.k8s.yaml").read_text()))
    app_config = one(
        doc for doc in staging_docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-app-config"
    )
    config = yaml.safe_load(app_config["data"]["config.yaml"])
    catalog = config["kubernetes_grants"]
    selected_grants = set(config["sandbox_presets"][preset]["kubernetes_grants"])
    reader_roles = {"agent-readable-namespace-metadata", "agent-readable-namespace-logs"}
    actual = {
        (grant["namespace"], grant["role_ref"]["name"])
        for name in selected_grants
        if (grant := catalog[name])["role_ref"]["name"] in reader_roles
    }
    assert actual == expected
    cleanup_namespaces = set(config["kubernetes_binding_cleanup_namespaces"])
    expected_namespaces = {namespace for namespace, _ in expected}
    expected_external_namespaces = expected_namespaces - {"agentplane-staging"}
    assert expected_external_namespaces <= cleanup_namespaces

    external_grant_namespaces = {grant["namespace"] for grant in catalog.values() if grant["kind"] == "RoleBinding"}
    expected_external_scopes = (external_grant_namespaces | cleanup_namespaces) - {"agentplane-staging"}
    assert expected_external_scopes == expected_external_namespaces | {"ducktape-flux", "haku-console", "haku-sandbox"}
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"]["name"] in {"agentplane-staging-managed-bindings", "agentplane-staging-external-bindings"}
        and doc["metadata"].get("namespace") != "agentplane-staging"
        for doc in staging_docs
    )

    for name in selected_grants:
        grant = catalog[name]
        if grant["role_ref"]["name"] in reader_roles:
            assert grant["kind"] == "RoleBinding"
            assert grant["role_ref"]["kind"] == "ClusterRole"

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
    # The clickhouse Namespace is emitted alongside the operator resources, but
    # its service Kustomization owns the namespace-scoped workload and grants.
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

        path = generated_dir / "agentplane/binding-delegation/agentplane-staging" / namespace
        objects = list(yaml.safe_load_all((path / f"{namespace}.k8s.yaml").read_text()))
        role = one(doc for doc in objects if doc["kind"] == "Role")
        binding = one(doc for doc in objects if doc["kind"] == "RoleBinding")
        delegation_role = "agentplane-staging-external-bindings"
        assert role["metadata"] == {"name": delegation_role, "namespace": namespace}
        assert role["rules"][0] == {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["rolebindings"],
            "verbs": ["create", "get", "list", "delete"],
        }
        assert {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles"], "verbs": ["get"]} in role["rules"]
        expected_bound_roles = {
            grant["role_ref"]["name"]
            for grant in catalog.values()
            if grant["kind"] == "RoleBinding"
            and grant["namespace"] == namespace
            and grant["role_ref"]["kind"] == "Role"
        }
        actual_bound_roles = {
            tuple(rule.get("resourceNames", []))
            for rule in role["rules"]
            if rule["resources"] == ["roles"] and rule["verbs"] == ["bind"]
        }
        assert actual_bound_roles == {(name,) for name in expected_bound_roles}
        assert binding["metadata"] == {"name": delegation_role, "namespace": namespace}
        assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": delegation_role}
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "agentplane-sandbox-service", "namespace": "agentplane-staging"}
        ]

        flux = flux_by_name[f"{delegation_name}{namespace}"]
        assert flux["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
        assert flux["spec"]["path"] == (
            f"./cluster/generated/agentplane/binding-delegation/agentplane-staging/{namespace}"
        )
        if expected_dependency is None:
            # Bootstrap roots have no generated construct representing their owner.
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


@pytest.fixture(scope="module")
def bootstrap_resources(k8s_dir: Path) -> list[K8sResource]:
    """Bootstrap roots are not children of the generated Flux graph; build them rather than guessing their labels."""
    bootstrap_roots = (
        parse_kustomize_file(k8s_dir / "flux/flux-system/kustomization.yaml"),
        flux_generated_kustomization(k8s_dir / "flux/ducktape-flux"),
    )
    return [resource for root in bootstrap_roots for resource in asyncio.run(run_kustomize_build(root)).resources]


@pytest.fixture(scope="module")
def agent_permissions(
    cluster: ParsedCluster, bootstrap_resources: list[K8sResource], repo_root: Path, k8s_dir: Path
) -> tuple[Rbac, dict]:
    # Active, rendered resources only.
    resources = [resource for group in cluster.flux_kust_resources(repo_root).values() for resource in group]
    resources.extend(bootstrap_resources)
    assert not any(r.kind == "ClusterPolicy" and r.name == "generate-agent-diagnostics-readers" for r in resources)
    assert not any(
        r.kind == "ClusterRole" and r.name == "kyverno-background-controller-rolebindings" for r in resources
    )
    assert not any(
        r.kind == "RoleBinding" and r.name in {"agent-readable-metadata", "agent-readable-logs"} for r in resources
    )
    # Compare binding scopes directly; broad Haku roles cannot mask omissions.
    # Labels are now descriptive output of the shared policy, not an access trigger.
    expected: set[tuple[str, str, str]] = set()
    for resource in resources:
        if resource.kind != "Namespace":
            continue
        labels = resource.metadata.labels
        if labels.get("rbac.ducktape.io/agent-readable-logs") == "true":
            expected.add((resource.name, "agent-diagnostics-metadata", "agent-readable-namespace-metadata"))
            expected.add((resource.name, "agent-diagnostics-logs", "agent-readable-namespace-logs"))
        elif labels.get("rbac.ducktape.io/agent-readable-metadata") == "true":
            expected.add((resource.name, "agent-diagnostics-metadata", "agent-readable-namespace-metadata"))
    actual: set[tuple[str, str, str]] = set()
    for resource in resources:
        if isinstance(resource, RoleBindingResource) and resource.name in {
            "agent-diagnostics-metadata",
            "agent-diagnostics-logs",
        }:
            assert resource.kind == "RoleBinding"
            assert resource.role_ref is not None
            assert resource.role_ref.kind == "ClusterRole"
            actual.add((resource.namespace, resource.name, resource.role_ref.name))
    assert actual == expected
    docs = yaml.safe_load_all((k8s_dir / "agentplane-staging/agentplane-staging.k8s.yaml").read_text())
    config = yaml.safe_load(
        one(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-app-config")[
            "data"
        ]["config.yaml"]
    )
    return Rbac(resources), config


def test_static_managed_public_coder_permission_parity(agent_permissions: tuple[Rbac, dict]) -> None:
    rbac, config = agent_permissions
    static = rbac.identity("Group", "haku:access-profile:public-coder")
    managed = rbac.managed(config, "public-coder", namespace="agentplane-staging")
    assert not uncovered(static, managed)
    assert not uncovered(managed, static)


def test_finance_spend_secret_role_is_named_get_only(agent_permissions: tuple[Rbac, dict]) -> None:
    rbac, config = agent_permissions
    role = config["kubernetes_grants"]["spend-private-config"]
    assert role == {
        "kind": "RoleBinding",
        "namespace": "plaid-mcp",
        "role_ref": {"kind": "Role", "name": "plaid-spend-finance-config-reader"},
    }
    assert rbac.rules(RbacRoleRef(api_group="rbac.authorization.k8s.io", **role["role_ref"]), role["namespace"]) == {
        Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-private-config")
    }


def test_agent_permission_superset_and_finance_parity(agent_permissions: tuple[Rbac, dict]) -> None:
    rbac, config = agent_permissions
    public = rbac.managed(config, "public-coder", namespace="agentplane-staging")
    haku = rbac.identity("Group", "haku:access-profile:haku")
    finance = rbac.managed(config, "finance-agent", namespace="agentplane-staging")
    assert not uncovered(public, haku)
    assert not uncovered(public, finance)
    assert uncovered(finance, public) == {
        Permission("agentplane-staging", "", "secrets", "get", "coinbase-api-credentials"),
        Permission("plaid-mcp", "", "secrets", "get", "plaid-spend-private-config"),
    }
    static_public = rbac.identity("Group", "haku:access-profile:public-coder")
    for kind, name, namespace in (
        ("Group", "oidc-ksbx-groups:haku", ""),
        ("Group", "haku:access-profile:haku", ""),
        ("ServiceAccount", "haku", "haku-sandbox"),
    ):
        # Haku's launch preset is paused, but its independent static identities remain.
        static_haku = rbac.identity(kind, name, namespace)
        assert not uncovered(static_public, static_haku), (kind, name, namespace)
        assert not uncovered(haku, static_haku), (kind, name, namespace)
        assert not uncovered(static_haku, haku), (kind, name, namespace)


@pytest.mark.parametrize("account", ["claude-ai", "haku-agent"])
def test_legacy_agentplane_accounts_are_not_haku_profile_aliases(
    agent_permissions: tuple[Rbac, dict], account: str
) -> None:
    rbac, config = agent_permissions
    # These OAuth/Actions accounts are NOT the Console profile or the managed
    # Haku preset. Preserve their independently scoped existing Kubernetes access.
    expected = rbac.rules(
        RbacRoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="agentplane-acceptance-token"),
        "agentplane-testing",
    )
    if account == "claude-ai":
        catalog = config["kubernetes_grants"]
        for key, grant in catalog.items():
            if key in {
                "cluster-diagnostics",
                "coinbase-credentials",
                "agentplane-testing-operator",
                "agentplane-testing-login",
                "haku-console-metadata",
                "clickhouse-diagnostics",
                "public-coder-agent-reader",
                "public-coder-volsync-status",
            } or grant["role_ref"]["name"] in {"agent-readable-namespace-metadata", "agent-readable-namespace-logs"}:
                expected.update(
                    rbac.rules(
                        RbacRoleRef(api_group="rbac.authorization.k8s.io", **grant["role_ref"]), grant.get("namespace")
                    )
                )
    actual = rbac.identity("ServiceAccount", account, "agentplane-staging")
    assert not uncovered(actual, expected)
    assert not uncovered(expected, actual)


def test_cluster_diagnostics_kustomizations_are_read_only(agent_permissions: tuple[Rbac, dict]) -> None:
    rbac, _ = agent_permissions
    rules = rbac.rules(
        RbacRoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="cluster-diagnostics-reader"), None
    )
    reads = {
        Permission(None, "kustomize.toolkit.fluxcd.io", "kustomizations", verb) for verb in ("get", "list", "watch")
    }
    writes = {
        Permission("flux-system", "kustomize.toolkit.fluxcd.io", "kustomizations", verb)
        for verb in ("create", "update", "patch", "delete", "deletecollection")
    }
    assert not uncovered(reads, rules)
    assert uncovered(writes, rules) == writes


def test_agent_permission_denials(agent_permissions: tuple[Rbac, dict]) -> None:
    rbac, config = agent_permissions
    profiles = {
        name: rbac.managed(config, name, namespace="agentplane-staging") for name in ("public-coder", "finance-agent")
    }
    profiles.update(
        {
            "claude-ai": rbac.identity("ServiceAccount", "claude-ai", "agentplane-staging"),
            "public-static": rbac.identity("Group", "haku:access-profile:public-coder"),
            "haku-console": rbac.identity("Group", "haku:access-profile:haku"),
            "haku-oidc": rbac.identity("Group", "oidc-ksbx-groups:haku"),
            "haku-sa": rbac.identity("ServiceAccount", "haku", "haku-sandbox"),
        }
    )
    denied = {
        Permission("public-coder-agent", "", "secrets", "get", "agentplane-acceptance-operator"),
        Permission("public-coder-agent", "", "secrets", "list"),
        Permission("public-coder-agent", "kubevirt.io", "virtualmachineinstances", "delete", "public-coder-devbox"),
        Permission("agentplane-staging", "", "pods/exec", "create"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "create"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "patch"),
        Permission("agentplane-staging", "agents.x-k8s.io", "sandboxes", "delete"),
    }
    denied |= {
        Permission(namespace, "kustomize.toolkit.fluxcd.io", "kustomizations", "patch")
        for namespace in ("flux-system", "ducktape-flux", "agentplane-testing", "agentplane-staging")
    }
    testing_login = Permission("public-coder-agent", "", "secrets", "get", "agentplane-testing-acceptance-operator")
    coinbase = Permission("agentplane-staging", "", "secrets", "get", "coinbase-api-credentials")
    denied |= {
        Permission("agentplane-staging", "", "secrets", verb)
        for verb in ("get", "list", "watch", "create", "update", "patch", "delete")
    }
    for name, permissions in profiles.items():
        assert bool(uncovered({coinbase}, permissions)) == (name in {"public-coder", "public-static"}), name
        assert uncovered(denied, permissions) == denied, name
        assert not uncovered({testing_login}, permissions), name
        if name == "claude-ai":
            sandbox_writes = {
                Permission("haku-sandbox", "", resource, verb)
                for resource in ("pods", "pods/exec", "secrets", "configmaps")
                for verb in ("create", "update", "patch", "delete")
            }
            assert uncovered(sandbox_writes, permissions) == sandbox_writes
        if name in {"public-coder", "finance-agent", "public-static"}:
            node_proxy = Permission(None, "", "nodes/proxy", "get")
            assert uncovered({node_proxy}, permissions), name


if __name__ == "__main__":
    pytest_bazel.main()
