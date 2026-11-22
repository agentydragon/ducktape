"""Integration tests: validate the real manifest trees via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources).
Use the pinned Flux CLI in dry-run mode for local owners with targetNamespace and
plain Kustomize for the other local owners. The latter builds omit Flux-level
patches. Live decryption and post-build variable sources are outside both paths.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_external_credential_ownership,
    check_external_secret_store_wiring,
    check_forgejo_image_namespace_reflection,
    check_goldilocks_namespace_labels,
    check_sops_decryption_blocks,
    find_orphaned_files,
)
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux import EXTERNAL_ARTIFACT_KIND, DependsOn, FluxKustomizationSpec, SourceRef
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.image_automation import (
    check_image_automation_webhook,
    check_image_policy_markers,
    check_no_flow_mappings_where_flux_writes,
)
from cluster.validation.k8s import AgentplaneSandboxTemplateResource, K8sResource, load_k8s_resources
from cluster.validation.kustomize import run_kustomize_build, stage_artifact_copies


async def _build_namespace_override(owner: str, spec: FluxKustomizationSpec, directory: Path) -> list[K8sResource]:
    assert spec.manifest_path is not None
    flux = Runfiles.CreateOrRaise().root() / "multitool/tools/flux/flux"
    process = await asyncio.create_subprocess_exec(
        flux,
        "build",
        "kustomization",
        owner,
        "--namespace",
        spec.namespace,
        "--kustomization-file",
        spec.manifest_path,
        "--path",
        directory,
        "--dry-run",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Flux build failed for {spec.namespace}/{owner}: {stderr.decode()}")
    return load_k8s_resources(stdout)


def _build_cluster(parsed: ParsedCluster, repo_root: Path) -> ParsedCluster:
    async def build(owner: str, spec: FluxKustomizationSpec, directory: Path) -> tuple[str, list[K8sResource]]:
        # Flux temporarily rewrites kustomization.yaml. Give each namespace override a private source,
        # even when another owner builds the same path with a different targetNamespace.
        with tempfile.TemporaryDirectory(prefix="flux-owner-") as scratch:
            source = Path(scratch)
            ref = spec.source_ref
            if ref and ref.kind == EXTERNAL_ARTIFACT_KIND:
                key = (ref.namespace or spec.namespace, ref.name)
                try:
                    stage_artifact_copies(repo_root, source, parsed.artifact_copies[key])
                except (ValueError, FileNotFoundError, KeyError) as error:
                    raise ValueError(f"{owner}: cannot stage ExternalArtifact {key}: {error}") from error
                directory = source / directory.relative_to(repo_root)
            elif spec.target_namespace:
                shutil.copytree(repo_root, source, dirs_exist_ok=True)
                directory = source / directory.relative_to(repo_root)

            if not directory.is_dir():
                raise FileNotFoundError(f"{owner}: {spec.path} is missing from its source")
            if spec.target_namespace:
                return owner, await _build_namespace_override(owner, spec, directory)

            return owner, await run_kustomize_build(directory)

    async def build_all() -> dict[str, list[K8sResource]]:
        return dict(
            await asyncio.gather(
                *(
                    build(owner, spec, directory)
                    for owner, spec in parsed.flux_kustomizations.items()
                    if (directory := spec.local_dir(repo_root)) is not None
                )
            )
        )

    return replace(parsed, rendered_resources=asyncio.run(build_all()))


@pytest.fixture(scope="session")
def parsed_cluster(repo_root: Path) -> ParsedCluster:
    return parse_cluster(repo_root)


@pytest.fixture(scope="session")
def cluster(parsed_cluster: ParsedCluster, repo_root: Path) -> ParsedCluster:
    """Build every local owner. A missing directory or failed build fails the fixture."""
    return _build_cluster(parsed_cluster, repo_root)


def test_flux_configmaps_and_secrets_have_namespaces(cluster: ParsedCluster) -> None:
    """A namespace-less generated ConfigMap prevented the public-coder Deployment from reconciling."""
    missing = [
        f"{owner}: {resource.kind}/{resource.name}"
        for owner, resources in cluster.flux_kust_resources(include_suspended=True).items()
        for resource in resources
        if resource.api_version == "v1" and resource.kind in {"ConfigMap", "Secret"} and not resource.namespace
    ]
    assert not missing, "Flux cannot apply resources missing a namespace:\n" + "\n".join(missing)


def test_no_dependency_errors(cluster: ParsedCluster) -> None:
    """Operator prerequisites are dependencies; every sourceRef resolves."""
    errors = validate_dependencies(cluster)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster) -> None:
    errors = check_controller_health_checks(cluster)
    assert not errors, "\n".join(errors)


def test_external_credential_ownership(cluster: ParsedCluster) -> None:
    """Only the shared store reads external-creds."""
    errors = check_external_credential_ownership(cluster)
    assert not errors, "\n".join(errors)


def test_external_secret_store_wiring(cluster: ParsedCluster) -> None:
    errors = check_external_secret_store_wiring(cluster)
    assert not errors, "\n".join(errors)


def test_external_secret_store_wiring_uses_each_flux_owners_target_namespace(tmp_path: Path) -> None:
    def write(relative: str, *documents: dict) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump_all(documents))

    def owner(name: str, directory: str, target_namespace: str | None = None) -> dict:
        spec = {"path": f"./cluster/generated/{directory}"}
        if target_namespace is not None:
            spec["targetNamespace"] = target_namespace
        return {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name, "namespace": "flux-system"},
            "spec": spec,
        }

    write(
        "cluster/k8s/flux/kustomizations.k8s.yaml",
        owner("first", "secrets", "allowed"),
        owner("second", "secrets", "denied"),
        owner("unspecified", "secrets"),
        owner("namespaces", "namespaces"),
        owner("policy", "policy", "operator"),
    )
    secret_spec = {
        "secretStoreRef": {"kind": "ClusterSecretStore", "name": "shared"},
        "data": [{"secretKey": "fixture", "remoteRef": {"key": "fixture"}}],
    }
    write(
        "cluster/generated/secrets/resources.yaml",
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {"name": "implicit"},
            "spec": secret_spec,
        },
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {"name": "explicit", "namespace": "allowed"},
            "spec": secret_spec,
        },
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "referent"}},
    )
    write(
        "cluster/generated/namespaces/resources.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "allowed", "labels": {"team": "approved"}}},
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "denied"}},
    )
    write(
        "cluster/generated/policy/resources.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "source-operator"}},
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ClusterSecretStore",
            "metadata": {"name": "shared"},
            "spec": {
                "conditions": [{"namespaceSelector": {"matchLabels": {"team": "approved"}}}],
                "provider": {"kubernetes": {"auth": {"serviceAccount": {"name": "referent"}}}},
            },
        },
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ClusterExternalSecret",
            "metadata": {"name": "cluster-consumer"},
            "spec": {"namespaces": ["allowed"], "externalSecretSpec": secret_spec},
        },
    )
    # The two targeted owners share one directory. The source Kustomization
    # also remains available to an owner with no namespace override.
    for directory in ("secrets", "namespaces", "policy"):
        write(
            f"cluster/generated/{directory}/kustomization.yaml",
            {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["resources.yaml"]},
        )

    parsed = _build_cluster(parse_cluster(tmp_path), tmp_path)
    builds = parsed.flux_kust_resources()
    assert {
        owner_name: {resource.namespace for resource in builds[owner_name] if resource.kind == "ExternalSecret"}
        for owner_name in ("first", "second", "unspecified")
    } == {"first": {"allowed"}, "second": {"denied"}, "unspecified": {"", "allowed"}}
    assert {resource.name: resource.namespace for resource in builds["policy"] if resource.kind == "Namespace"} == {
        "operator": ""
    }
    errors = check_external_secret_store_wiring(parsed)
    assert len(errors) == 2
    for secret in ("implicit", "explicit"):
        [error] = [
            error
            for error in errors
            if f"ExternalSecret '{secret}' in namespace 'denied' is denied by ClusterSecretStore" in error
        ]
        assert "second" in error
        assert "./cluster/generated/secrets" in error


def test_session_archive_importers_receive_the_backup_credentials(
    cluster: ParsedCluster, repo_root: Path, tmp_path: Path
) -> None:
    owner = cluster.flux_kustomizations["ai-session-archive"]
    # Flux dry-run masks the ciphertext even if the owner has no decryption key.
    assert owner.decryption is not None
    assert owner.decryption.provider == "sops"
    assert owner.decryption.secret_ref is not None
    # persistent-auth.tf provisions this key and reflects it into ducktape-flux.
    assert owner.decryption.secret_ref.name == "sops-age-cluster-secrets"
    source = owner.source_ref
    assert source is not None
    stage_artifact_copies(
        repo_root, tmp_path, cluster.artifact_copies[source.namespace or owner.namespace, source.name]
    )
    # Flux dry-run masks SOPS payloads. Use Kustomize here to verify which file the importer actually receives.
    kustomize = Runfiles.CreateOrRaise().root() / "multitool/tools/kustomize/kustomize"
    output = subprocess.check_output([kustomize, "build", tmp_path / owner.path])
    resources = list(yaml.load_all(output, Loader=yaml.CSafeLoader))
    [secret] = [doc for doc in resources if doc["kind"] == "Secret"]
    jobs = [doc for doc in resources if doc["kind"] == "CronJob"]
    assert jobs
    expected = (repo_root / "secrets/shared/session-backup.yaml").read_bytes()
    for job in jobs:
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        [importer] = [container for container in pod["containers"] if "--credentials-file" in container.get("args", [])]
        path = Path(importer["args"][importer["args"].index("--credentials-file") + 1])
        [mount] = [mount for mount in importer["volumeMounts"] if path.is_relative_to(mount["mountPath"])]
        [volume] = [volume["secret"] for volume in pod["volumes"] if volume["name"] == mount["name"]]
        assert (job["metadata"]["namespace"], volume["secretName"]) == (
            secret["metadata"]["namespace"],
            secret["metadata"]["name"],
        )
        key = str(path.relative_to(mount["mountPath"]))
        if "items" in volume:
            [key] = [item["key"] for item in volume["items"] if item["path"] == key]
        assert base64.b64decode(secret["data"][key]) == expected


def test_staging_sandbox_templates_use_their_pinned_images(cluster: ParsedCluster, k8s_dir: Path) -> None:
    component = yaml.safe_load((k8s_dir / "agentplane-staging/image-pins/kustomization.yaml").read_text())
    pins = {entry["name"]: entry["newTag"] for entry in component["images"]}
    templates = [
        resource
        for resource in cluster.rendered_resources["agentplane-staging"]
        if isinstance(resource, AgentplaneSandboxTemplateResource)
    ]
    assert templates
    for template in templates:
        assert template.images, template.name
        for image in template.images:
            name, _, tag = image.rpartition(":")
            assert tag != "unset", (template.name, image)
            if name in pins:
                assert tag == pins[name], (template.name, image)


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


def test_agent_binding_delegation_dependencies_follow_namespace_owners(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Delegated binding applies follow the namespace owner that creates their targets."""
    retained_resources = cluster.flux_kust_resources(include_suspended=True)
    namespace_owners: dict[str, set[str]] = {}
    for owner, resources in retained_resources.items():
        for resource in resources:
            if resource.kind != "Namespace":
                continue
            namespace_owners.setdefault(resource.name, set()).add(owner)

    staging = k8s_dir / "agentplane-staging/agentplane-staging.k8s.yaml"
    policy_set_namespaces = {
        namespace
        for doc in yaml.safe_load_all(staging.read_text())
        if doc["kind"] == "PolicySet"
        for rule in doc["spec"]["rules"]
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

    prefix = "agentplane-staging-binding-delegation-"
    delegations = {
        owner.removeprefix(prefix): owner for owner in cluster.flux_kustomizations if owner.startswith(prefix)
    }
    assert delegations.keys() == expected_external_scopes

    # Haku and ClickHouse gate delegation on a later reconciliation step.
    namespace_owners.update({"haku-sandbox": {"haku-rbac"}, "clickhouse": {"clickhouse"}})
    source = SourceRef(kind="GitRepository", name="ducktape", namespace="ducktape-flux")
    for namespace, owner in delegations.items():
        spec = cluster.flux_kustomizations[owner]
        assert spec.source_ref == source, owner
        assert {resource.namespace for resource in retained_resources[owner] if resource.kind == "RoleBinding"} == {
            namespace
        }, owner
        if namespace in {"flux-system", "ducktape-flux"}:
            assert not spec.depends_on, owner
        else:
            [dependency] = namespace_owners[namespace]
            assert spec.depends_on == [DependsOn(name=dependency, namespace="ducktape-flux")], owner


def test_files_flux_rewrites_use_block_style(k8s_dir: Path) -> None:
    """A flow mapping in a file Flux rewrites fails prettier on every open PR at once.

    Flux re-serialises the whole document to set a tag, dropping prettier's inner spaces, and
    that lands on `devel` with `[skip ci]` — so the failure surfaces in unrelated PRs.
    """
    errors = check_no_flow_mappings_where_flux_writes(k8s_dir)
    assert not errors, "\n".join(errors)


def test_sops_secrets_have_decryption_block(cluster: ParsedCluster) -> None:
    """Active flux kustomizations rendering a SOPS Secret must declare decryption.provider: sops."""
    errors = check_sops_decryption_blocks(cluster)
    assert not errors, "\n".join(errors)


def test_retry_policy(parsed_cluster: ParsedCluster) -> None:
    errors = check_retry_policy(parsed_cluster)
    assert not errors, "\n".join(errors)


def test_no_orphaned_files(parsed_cluster: ParsedCluster, repo_root: Path) -> None:
    """All active YAML files must be referenced by a kustomization.yaml."""
    errors = find_orphaned_files(parsed_cluster, repo_root)
    assert not errors, "\n".join(errors)


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """A namespace with a goldilocks vpa-update-mode is not opted out of goldilocks."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> None:
    """No Cilium policy rule with all rule sections empty (Cilium rejects it, silently unenforced)."""
    errors = check_cilium_policy_rules_nonempty(cluster)
    assert not errors, "\n".join(errors)


def test_no_legacy_agent_diagnostic_rbac(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """The delivered cluster has no implicit Kyverno-generated agent readers."""
    # Suspended owners retain their grants. The two bootstrap roots are outside the Flux graph.
    owners = cluster.flux_kust_resources(include_suspended=True)
    for root in ("flux/flux-system", "flux/ducktape-flux"):
        owners[root] = asyncio.run(run_kustomize_build(k8s_dir / root))

    retired = {
        ("ClusterPolicy", "generate-agent-diagnostics-readers"),
        ("ClusterRole", "kyverno-background-controller-rolebindings"),
        ("RoleBinding", "agent-readable-metadata"),
        ("RoleBinding", "agent-readable-logs"),
    }
    assert not [
        (owner, resource.kind, resource.namespace, resource.name)
        for owner, resources in owners.items()
        for resource in resources
        if (resource.kind, resource.name) in retired
    ]


if __name__ == "__main__":
    pytest_bazel.main()
