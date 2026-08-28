"""Integration tests: validate real cluster/k8s/ config via pure analysis.

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

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_duplicate_external_secrets,
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    check_sops_decryption_blocks,
    find_orphaned_files,
)
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.image_automation import (
    check_image_automation_webhook,
    check_image_policy_markers,
    check_no_flow_mappings_where_flux_writes,
)
from cluster.validation.kustomize import KustomizeBuildResult, run_kustomize_build
from cluster.validation.postbuild_substitutions import check_postbuild_substitution_sources
from cluster.validation.terraform_backends import check_terraform_backends
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def _local_flux_kust_names(parsed: ParsedCluster, k8s_dir: Path) -> set[str]:
    """Active flux kustomization names whose spec.path points into the local cluster/k8s tree."""
    return {name for name, spec in parsed.active_flux_kustomizations.items() if spec.local_dir(k8s_dir)}


@pytest.fixture(scope="session")
def cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse cluster and build flux-referenced kustomizations (hard failure on any build error)."""
    parsed = parse_cluster(k8s_dir)

    # Build all local flux-referenced kustomizations (including suspended — kustomize
    # build should still succeed). Only validation checks filter suspended.
    local_dirs = {d for spec in parsed.flux_kustomizations.values() if (d := spec.local_dir(k8s_dir))}
    kust_files = [k for k in parsed.kustomize_files if k.parent.resolve() in local_dirs]

    async def _build_all() -> list[KustomizeBuildResult]:
        return list(await asyncio.gather(*[run_kustomize_build(k) for k in kust_files]))

    parsed.build_results = asyncio.run(_build_all())
    return parsed


def test_all_local_flux_kustomizations_have_build_results(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every flux kustomization pointing to a local path must have a build result."""
    covered = set(cluster.flux_kust_resources(k8s_dir))
    expected = _local_flux_kust_names(cluster, k8s_dir)
    missing = sorted(expected - covered)
    assert not missing, "Flux kustomizations with no build result:\n" + "\n".join(f"  {m}" for m in missing)


def test_no_dependency_errors(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """No cycles, required dependencies present, operator dependencies satisfied."""
    errors = validate_dependencies(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster, k8s_dir: Path) -> None:
    errors = check_controller_health_checks(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_no_crd_layering_violations(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Active kustomizations must not mix HelmReleases with external-operator CRD instances."""
    active_dirs = {spec.local_dir(k8s_dir) for spec in cluster.active_flux_kustomizations.values()}
    errors: list[str] = []
    for result in cluster.build_results:
        if result.kustomization_path.parent.resolve() not in active_dirs:
            continue
        try:
            check_crd_layering(result)
        except CrdLayeringViolationError as e:
            errors.append(str(e))
    assert not errors, "\n".join(errors)


def test_single_external_secrets_installation(cluster: ParsedCluster) -> None:
    """Exactly one external-secrets HelmRelease across the cluster."""
    errors = check_duplicate_external_secrets(cluster.build_results)
    assert not errors, "\n".join(errors)


def test_terraform_backends_not_kubernetes(cluster: ParsedCluster) -> None:
    """tofu-controller Terraform CRs must use the pg backend, not kubernetes Secrets."""
    errors = check_terraform_backends(cluster)
    assert not errors, "\n".join(errors)


def test_image_automation_webhook_consistency(cluster: ParsedCluster) -> None:
    """Every rendered ImageRepository is in the webhook Receiver; every ImagePolicy ref resolves.

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
    cluster: ParsedCluster, k8s_dir: Path
) -> None:
    """A Namespace opt-in for Kubernetes pod logs must also permit its Loki logs.

    The proxy remains static and network-isolated; this CI contract makes the
    GitOps-owned logs label the review point for extending that static policy.
    """
    deployment = yaml.safe_load((k8s_dir / "agents/loki-read-proxy/deployment.yaml").read_text())
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
    flux_system_kustomization = (k8s_dir / "flux-system/kustomization.yaml").read_text()
    assert "path: /metadata/labels/rbac.ducktape.io~1agent-readable-logs" in flux_system_kustomization
    labeled_namespaces.add("flux-system")

    missing = sorted(labeled_namespaces - loki_allowlist)
    assert not missing, f"agent-readable log namespaces missing from Loki proxy allowlist: {missing}"


def test_analytics_clickhouse_distributed_ddl_contract(k8s_dir: Path) -> None:
    """Analytics uses one plaintext native port consistently for ON CLUSTER DDL.

    The Altinity operator generates 9440 secure remote-server entries when
    ``secure: true`` is set, but ClickHouse has no TLS listener unless one is
    configured separately. A 9440 remote entry therefore cannot be recognized
    as local by DDLWorker. Keep the manifest, schema Job, and Flux health check
    pinned to the working port-9000 configuration.
    """
    analytics_dir = k8s_dir / "analytics"
    installation = yaml.safe_load((analytics_dir / "cluster/clickhouse.yaml").read_text())
    configuration = installation["spec"]["configuration"]
    cluster_spec = next(item for item in configuration["clusters"] if item["name"] == "analytics")
    assert "secure" not in cluster_spec
    assert installation["spec"]["defaults"]["replicasUseFQDN"] == "yes"

    grants = configuration["users"]["aiquota_ingest/grants/query"]
    assert grants == [
        "GRANT INSERT ON aiquota.raw_http_observations",
        "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits) "
        "ON aiquota.raw_http_observations",
    ]

    cluster_kustomization = yaml.safe_load((analytics_dir / "cluster/kustomization.yaml").read_text())
    generated_files = cluster_kustomization["configMapGenerator"][0]["files"]
    assert generated_files == ["system_logs.xml"]

    schema_sql = (analytics_dir / "schema/schema.sql").read_text()
    for statement in (
        "CREATE DATABASE IF NOT EXISTS aiquota ON CLUSTER analytics;",
        "CREATE TABLE IF NOT EXISTS aiquota.raw_http_observations ON CLUSTER analytics",
        "CREATE TABLE IF NOT EXISTS aiquota.aiquota_windows ON CLUSTER analytics",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.aiquota_windows_mv ON CLUSTER analytics",
        "CREATE TABLE IF NOT EXISTS aiquota.token_activity_daily ON CLUSTER analytics",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.token_activity_daily_mv ON CLUSTER analytics",
        "CREATE TABLE IF NOT EXISTS aiquota.reset_credits ON CLUSTER analytics",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.reset_credits_mv ON CLUSTER analytics",
    ):
        assert statement in schema_sql

    schema_job = yaml.safe_load((analytics_dir / "schema/schema-job.yaml").read_text())
    assert schema_job["metadata"]["name"] == "clickhouse-aiquota-schema-v6"
    schema_args = schema_job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--host=clickhouse-analytics.analytics.svc.cluster.local" in schema_args
    assert "--port=9000" in schema_args
    assert not any(item.startswith("chi-analytics-analytics-") for item in schema_args)

    schema_flux = yaml.safe_load((analytics_dir / "schema/flux-kustomization.yaml").read_text())
    assert schema_flux["spec"]["healthChecks"] == [
        {"apiVersion": "batch/v1", "kind": "Job", "name": "clickhouse-aiquota-schema-v6", "namespace": "analytics"}
    ]


def test_public_coder_clickhouse_reader_contract(k8s_dir: Path) -> None:
    """The Console-managed public-coder runner gets a mediated native ClickHouse reader.

    The app has only a placeholder, while the real password is reflected into
    its Iron proxy. Both ClickHouse and Cilium constrain the resulting query
    surface; 8123 remains an internal ClusterIP port, not a Gateway route.
    """
    analytics_dir = k8s_dir / "analytics" / "cluster"
    agent_dir = k8s_dir / "agents" / "public-coder-agent"

    installation = yaml.safe_load((analytics_dir / "clickhouse.yaml").read_text())
    users = installation["spec"]["configuration"]["users"]
    assert users["public_coder_analytics/password"]["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-public-coder-credentials",
        "key": "password",
    }
    assert users["public_coder_analytics/profile"] == "readonly"
    assert users["public_coder_analytics/quota"] == "readonly"
    assert users["public_coder_analytics/grants/query"] == [
        "GRANT SELECT ON aiquota.aiquota_windows",
        "GRANT SELECT ON aiquota.raw_http_observations",
    ]

    source_secret = yaml.safe_load((analytics_dir / "public-coder-credentials.sops.yaml").read_text())
    annotations = source_secret["metadata"]["annotations"]
    assert source_secret["metadata"]["namespace"] == "analytics"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == "public-coder-agent"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == "public-coder-agent"

    app = yaml.safe_load((agent_dir / "app/deployment.yaml").read_text())
    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    assert app_env["CLICKHOUSE_PUBLIC_CODER_USER"] == "public_coder_analytics"
    assert app_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"] == "proxy-clickhouse-public-coder-password"
    assert app_env["NO_PROXY"] == "127.0.0.1,localhost,litellm.litellm.svc,litellm.litellm.svc.cluster.local"

    proxy = yaml.safe_load((agent_dir / "proxy/deployment.yaml").read_text())
    proxy_env = {entry["name"]: entry for entry in proxy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert proxy_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-public-coder-credentials",
        "key": "password",
    }

    iron = yaml.safe_load((agent_dir / "proxy/iron.yaml").read_text())
    secrets = next(transform for transform in iron["transforms"] if transform["name"] == "secrets")["config"]["secrets"]
    clickhouse_secret = next(
        secret for secret in secrets if secret["source"]["var"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert clickhouse_secret["replace"] == {
        "proxy_value": "proxy-clickhouse-public-coder-password",
        "match_headers": ["Authorization"],
    }
    assert clickhouse_secret["rules"] == [{"host": "clickhouse-analytics.analytics.svc.cluster.local"}]

    policies = list(yaml.safe_load_all((analytics_dir / "networkpolicy.yaml").read_text()))
    clickhouse_ingress = next(policy for policy in policies if policy["metadata"]["name"] == "clickhouse-ingress")
    clickhouse_rule = next(
        rule
        for rule in clickhouse_ingress["spec"]["ingress"]
        if rule.get("from")
        == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "public-coder-agent"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "public-coder-agent-proxy"}},
            }
        ]
    )
    assert clickhouse_rule["ports"] == [{"port": 8123, "protocol": "TCP"}]

    proxy_egress = yaml.safe_load((agent_dir / "proxy/cnp-egress.yaml").read_text())
    assert {
        "toEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": "analytics",
                    "k8s:app.kubernetes.io/name": "clickhouse",
                    "k8s:app.kubernetes.io/instance": "analytics",
                }
            }
        ],
        "toPorts": [{"ports": [{"port": "8123", "protocol": "TCP"}]}],
    } in proxy_egress["spec"]["egress"]


def test_files_flux_rewrites_use_block_style(k8s_dir: Path) -> None:
    """A flow mapping in a file Flux rewrites fails prettier on every open PR at once.

    Flux re-serialises the whole document to set a tag, dropping prettier's inner spaces, and
    that lands on `devel` with `[skip ci]` — so the failure surfaces in unrelated PRs.
    """
    errors = check_no_flow_mappings_where_flux_writes(k8s_dir)
    assert not errors, "\n".join(errors)


def test_flux_bootstrap_auth_split(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Cold bootstrap sources must not depend on Flux-decrypted auth; write sources must."""
    errors = check_flux_bootstrap_auth(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_sops_secrets_have_decryption_block(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Active flux kustomizations rendering a SOPS Secret must declare decryption.provider: sops."""
    errors = check_sops_decryption_blocks(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_postbuild_substitution_sources_are_namespace_local(cluster: ParsedCluster) -> None:
    """postBuild ConfigMaps/Secrets must be local or explicitly auto-reflected."""
    errors = check_postbuild_substitution_sources(cluster)
    assert not errors, "\n".join(errors)


def test_retry_policy(cluster: ParsedCluster) -> None:
    check_retry_policy(cluster)


def test_no_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """All YAML files must be referenced by a kustomization.yaml."""
    errors = find_orphaned_files(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_no_unwired_flux_kustomizations(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every flux-kustomization.yaml on disk must be referenced in the root kustomization."""
    on_disk = {f.resolve() for f in k8s_dir.rglob("flux-kustomization.yaml") if "flux-system" not in f.parts}

    root_kust = cluster.kustomize_files[k8s_dir / "kustomization.yaml"]
    referenced = {r for r in root_kust.resolved_resources if r.name == "flux-kustomization.yaml"}

    unwired = sorted(f.relative_to(k8s_dir) for f in on_disk - referenced)
    assert not unwired, "flux-kustomization.yaml files not listed in root kustomization.yaml:\n" + "\n".join(
        f"  {f}" for f in unwired
    )


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """Namespaces with goldilocks vpa-update-mode must also have goldilocks enabled."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_goldilocks_explicit_decision(cluster: ParsedCluster) -> None:
    """Namespaces with workloads must explicitly set goldilocks enabled label."""
    errors = check_goldilocks_explicit_decision(cluster)
    assert not errors, "\n".join(errors)


def test_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> None:
    """No Cilium policy rule with all rule sections empty (Cilium rejects it, silently unenforced)."""
    errors = check_cilium_policy_rules_nonempty(cluster)
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
