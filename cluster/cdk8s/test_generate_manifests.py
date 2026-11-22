"""Pinning tests for the cdk8s manifest generators.

The generated-output snapshot (STYLE.md § Testing) over every file the generator writes:
the committed files are the source of truth, and regeneration must reproduce them exactly.
`GENERATED_ROOT` is closed in the other direction too, so a hand-added or stale file there
fails; generated files beside hand-written ones under `HAND_WRITTEN_ROOT` or `PARKED_ROOT`
are pinned only one way.
"""

import os
from difflib import unified_diff
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT, PARKED_ROOT
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS
from cluster.cdk8s.testing.manifests import find_resource
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

type YamlDocuments = dict[Path, list[dict[str, Any]]]

_REGENERATE = "regenerate with `bb run //cluster/cdk8s:generate_manifests` and commit the result"


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def generated(runfiles: Runfiles) -> Path:
    root_path = runfiles.Rlocation(os.environ["GENERATED_MANIFESTS_RLOCATION"])
    assert root_path is not None
    return Path(root_path)


@pytest.fixture(scope="module")
def parsed_yaml_documents(generated: Path) -> YamlDocuments:
    """YAML mappings by repository-relative path."""
    return {
        path.relative_to(generated): [
            doc for doc in yaml.load_all(path.read_text(), Loader=yaml.CSafeLoader) if isinstance(doc, dict)
        ]
        for path in generated.rglob("*.yaml")
    }


@pytest.fixture(scope="module")
def active_yaml_documents(parsed_yaml_documents: YamlDocuments) -> YamlDocuments:
    """Files written by cdk8s into the active Flux roots."""
    return {
        path: docs
        for path, docs in parsed_yaml_documents.items()
        if path.is_relative_to(GENERATED_ROOT)
        or (path.is_relative_to(HAND_WRITTEN_ROOT) and path.name.endswith(".k8s.yaml"))
    }


@pytest.fixture(scope="module")
def documents(active_yaml_documents: YamlDocuments) -> list[dict[str, Any]]:
    return [doc for docs in active_yaml_documents.values() for doc in docs]


@pytest.fixture(scope="module")
def active_api_objects(active_yaml_documents: YamlDocuments) -> list[dict[str, Any]]:
    """API objects, excluding local Kustomize configuration."""
    return [doc for path, docs in active_yaml_documents.items() if path.name.endswith(".k8s.yaml") for doc in docs]


def test_auto_vpa_namespaces_declare_a_resource_policy(documents: list[dict[str, Any]], checkout: Path) -> None:
    # Parked third-party applications keep their hand-written manifests. Check those too,
    # so reviving one does not silently lose the default when the admission rule is retired.
    parked = [
        doc
        for path in (checkout / PARKED_ROOT).rglob("*.yaml")
        for doc in yaml.load_all(path.read_text(), Loader=yaml.CSafeLoader)
        if isinstance(doc, dict)
    ]
    namespaces = [
        doc
        for doc in [*documents, *parked]
        if doc.get("kind") == "Namespace"
        and doc.get("metadata", {}).get("labels", {}).get("goldilocks.fairwinds.com/vpa-update-mode") == "auto"
    ]
    assert namespaces
    missing = [
        doc["metadata"]["name"]
        for doc in namespaces
        if "goldilocks.fairwinds.com/vpa-resource-policy" not in doc["metadata"].get("annotations", {})
    ]
    assert not missing, f"auto VPA namespaces missing a resource policy: {missing}"


@pytest.fixture(scope="module")
def worker_pool(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return find_resource(documents, "VirtualMachinePool", "buildbuddy-kvm-worker", namespace="buildbuddy-executor")


def test_kvm_worker_pool_members_are_selected_fenced_and_given_their_own_ssd_disk(
    documents: list[dict[str, Any]], worker_pool: dict[str, Any]
) -> None:
    # /scale cannot read the pool if replicas is absent, even though its controller defaults to one.
    assert "replicas" in worker_pool["spec"]
    name = worker_pool["metadata"]["name"]
    namespace = worker_pool["metadata"]["namespace"]
    scaler = find_resource(documents, "ScaledObject", name, namespace=namespace)
    target = scaler["spec"]["scaleTargetRef"]
    assert target == {"apiVersion": worker_pool["apiVersion"], "kind": worker_pool["kind"], "name": name}
    group, version = target["apiVersion"].split("/", 1)
    flux = find_resource(documents, "Kustomization", name)
    assert flux["spec"]["ignore"] == [
        {
            "paths": ["/spec/replicas"],
            "target": {
                "group": group,
                "version": version,
                "kind": target["kind"],
                "name": name,
                "namespace": namespace,
            },
        }
    ]
    template = worker_pool["spec"]["virtualMachineTemplate"]
    labels = worker_pool["spec"]["selector"]["matchLabels"]
    # A template change must not replace every worker at once, which would leave the pool without an executor.
    assert worker_pool["spec"]["maxUnavailable"] == 1
    assert labels.items() <= template["metadata"]["labels"].items()
    assert labels.items() <= template["spec"]["template"]["metadata"]["labels"].items()
    guest = template["spec"]["template"]["spec"]
    # Wyrm2 is outside the OVH region, but workers still require nested virtualization.
    assert "topology.kubernetes.io/region" not in guest.get("nodeSelector", {})
    # A member is ready only once its executor has registered; the readiness endpoint is the executor's own.
    probe = guest["readinessProbe"]["httpGet"]
    assert probe["path"] == "/readyz"
    assert {"name": "server-type", "value": "buildbuddy-executor"} in probe["httpHeaders"]
    (work,) = template["spec"]["dataVolumeTemplates"]
    storage = work["spec"]["storage"]
    assert storage["storageClassName"] == "piraeus-ssd-local-delete"
    assert storage["volumeMode"] == "Filesystem"
    (disk_volume,) = [volume for volume in guest["volumes"] if volume["name"] == "work"]
    assert disk_volume["dataVolume"]["name"] == work["metadata"]["name"]
    # The egress fence selects pool members by the shared worker label.
    fence = next(
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy"
        and doc["metadata"]["name"].startswith("buildbuddy-kvm-worker")
        and doc["spec"]["endpointSelector"]["matchLabels"] == labels
    )["spec"]
    assert fence["egress"], fence
    assert all(not rule.get("toEntities") and not rule.get("toCIDR") for rule in fence["egress"])


def test_kvm_worker_metrics_are_scrapable_only_by_node_local_alloy(
    documents: list[dict[str, Any]], worker_pool: dict[str, Any]
) -> None:
    service = find_resource(documents, "Service", "buildbuddy-kvm-worker-metrics")
    monitor = find_resource(documents, "ServiceMonitor", "buildbuddy-kvm-worker")
    ports = {port["name"]: port for port in service["spec"]["ports"]}
    pod_labels = worker_pool["spec"]["virtualMachineTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert service["spec"]["selector"].items() <= pod_labels.items()
    assert monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
    assert {endpoint["port"] for endpoint in monitor["spec"]["endpoints"]} == ports.keys()
    for policy in (
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"].startswith("buildbuddy-kvm-worker")
    ):
        (rule,) = policy["spec"]["ingress"]
        assert rule["fromEndpoints"] == [
            {"matchLabels": {"app.kubernetes.io/name": "alloy-node", "k8s:io.kubernetes.pod.namespace": "monitoring"}}
        ]
        assert {p["port"] for p in rule["toPorts"][0]["ports"]} == {str(port["port"]) for port in ports.values()}


def test_kvm_worker_keeps_privileged_actions_inside_the_guest(
    documents: list[dict[str, Any]], worker_pool: dict[str, Any]
) -> None:
    worker = worker_pool["spec"]["virtualMachineTemplate"]["spec"]
    guest = worker["template"]["spec"]
    assert worker["runStrategy"] == "Always"
    kubevirt = next(doc["spec"] for doc in documents if doc["kind"] == "KubeVirt")
    control_plane_toleration = {
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
        "effect": "NoSchedule",
    }
    assert control_plane_toleration in guest["tolerations"]
    assert control_plane_toleration in kubevirt["infra"]["nodePlacement"]["tolerations"]
    assert "kubernetes.io/hostname" not in kubevirt["infra"]["nodePlacement"].get("nodeSelector", {})
    workload_placement = kubevirt["workloads"]["nodePlacement"]
    assert control_plane_toleration in workload_placement["tolerations"]
    assert not workload_placement.get("nodeSelector")
    assert not workload_placement.get("affinity")
    assert guest["domain"]["cpu"]["model"] == "host-passthrough"
    features = {f["name"] for f in guest["domain"]["cpu"]["features"] if f["policy"] == "optional"}
    assert features == {"svm", "vmx"}
    terms = guest["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    # Separate terms are alternatives: Intel or AMD, never both on the same host.
    assert len(terms) == len(features)
    for feature in features:
        assert {
            "matchExpressions": [
                {"key": f"cpu-feature.node.kubevirt.io/{feature}", "operator": "In", "values": ["true"]}
            ]
        } in terms
    assert not guest["domain"]["devices"].get("hostDevices")
    assert not guest["domain"]["devices"].get("gpus")
    assert not any("hostDisk" in volume or "serviceAccount" in volume for volume in guest["volumes"])
    # These serials are the prebuilt guest's mount/credential contract.
    disks = {disk.get("serial"): disk["name"] for disk in guest["domain"]["devices"]["disks"]}
    volumes = {volume["name"]: volume for volume in guest["volumes"]}
    assert volumes[disks["bbkey"]]["secret"]["secretName"] == "buildbuddy-api-key"
    assert "dataVolume" in volumes[disks["work"]]
    # Runtime bootstrap belongs in the immutable image; inline cloud-init could
    # reintroduce package downloads or a configuration reboot during scale-up.
    assert not any("cloudInitNoCloud" in volume for volume in guest["volumes"])
    config_map = find_resource(documents, "ConfigMap", "buildbuddy-kvm-worker")
    assert volumes[disks["bbconfig"]]["configMap"]["name"] == config_map["metadata"]["name"]
    policy = find_resource(documents, "CiliumNetworkPolicy", "buildbuddy-kvm-worker")["spec"]
    # Ingress admits only named peers (the metrics scraper); the exact peer and port are pinned in
    # test_kvm_worker_metrics_are_scrapable_only_by_node_local_alloy.
    assert all(
        rule.get("fromEndpoints") and not rule.get("fromEntities") and not rule.get("fromCIDR")
        for rule in policy["ingress"]
    )


@pytest.fixture(scope="module")
def checkout(runfiles: Runfiles) -> Path:
    """The runfiles tree, which holds each committed file the data deps package at its repo path."""
    return Path(str(runfiles.root() / "ducktape/MODULE.bazel")).parent


def test_generated_tree_matches_committed_snapshot(generated: Path, checkout: Path) -> None:
    generated_files = _files(generated)
    missing: list[str] = []
    stale: list[str] = []
    patch: list[str] = []
    for relative in sorted(generated_files):
        committed_path = checkout / relative
        before = committed_path.read_text() if committed_path.is_file() else None
        after = (generated / relative).read_text()
        if before == after:
            continue
        (missing if before is None else stale).append(relative)
        patch.extend(
            unified_diff(
                (before or "").splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="/dev/null" if before is None else f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    # CI uses the pinned generator, so its diff can be applied directly to repair drift.
    if patch:
        (undeclared_outputs_dir() / "generated-manifests.patch").write_text("".join(patch))

    committed = {f"{GENERATED_ROOT}/{relative}" for relative in _files(checkout / GENERATED_ROOT)}
    extra = sorted(committed - generated_files)

    failures = []
    if missing:
        failures.append(f"Generated but not committed ({_REGENERATE}):\n" + "\n".join(missing))
    if stale:
        failures.append(f"Stale ({_REGENERATE}):\n" + "\n".join(stale))
    if extra:
        failures.append(
            f"Committed under {GENERATED_ROOT} but not written by the generator; delete it, or keep a "
            "directory holding a hand-written file whole under the hand-written root:\n" + "\n".join(extra)
        )

    assert not failures, "\n\n".join(failures)


def test_piraeus_flux_retains_storage_and_waits_for_prerequisites(documents: list[dict[str, Any]]) -> None:
    operator_flux = find_resource(documents, "Kustomization", "piraeus-operator")
    cluster_flux = find_resource(documents, "Kustomization", "piraeus-cluster")
    assert operator_flux["spec"]["prune"] is False
    assert operator_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["prune"] is False
    assert cluster_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["dependsOn"] == [
        {"name": "piraeus-operator", "namespace": "ducktape-flux"},
        {"name": "cert-manager", "namespace": "ducktape-flux"},
        {"name": "monitoring-crds", "namespace": "ducktape-flux"},
    ]


def test_crossplane_core_waits_for_its_controller_and_retains_its_inventory(documents: list[dict[str, Any]]) -> None:
    release = find_resource(documents, "HelmRelease", "crossplane")
    kustomization = find_resource(documents, "Kustomization", "crossplane")

    assert release["metadata"]["namespace"] == "crossplane-system"
    assert kustomization["spec"]["dependsOn"] == [{"name": "kyverno", "namespace": "ducktape-flux"}]
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["prune"] is False


def test_private_crossplane_providers_can_pull_from_forgejo(documents: list[dict[str, Any]], checkout: Path) -> None:
    # The registry tenant owns the credential. Crossplane pulls provider packages in its installation namespace.
    source = yaml.safe_load((checkout / HAND_WRITTEN_ROOT / "forgejo-images/registry-creds.sops.yaml").read_text())
    source_name = source["metadata"]["name"]
    source_namespace = source["metadata"]["namespace"]
    namespace = find_resource(documents, "HelmRelease", "crossplane")["metadata"]["namespace"]
    mirror = find_resource(documents, "ExternalSecret", source_name, namespace=namespace)["spec"]
    assert mirror["dataFrom"] == [{"extract": {"key": source_name}}]
    assert mirror["target"]["template"]["type"] == source["type"] == "kubernetes.io/dockerconfigjson"

    ref = mirror["secretStoreRef"]
    store = find_resource(documents, ref["kind"], ref["name"])["spec"]
    assert store["conditions"] == [{"namespaces": [namespace]}]
    kubernetes = store["provider"]["kubernetes"]
    assert kubernetes["remoteNamespace"] == source_namespace
    reader = kubernetes["auth"]["serviceAccount"]
    account_namespace = reader.get("namespace", namespace)
    find_resource(documents, "ServiceAccount", reader["name"], namespace=account_namespace)
    rbac = Rbac(doc for doc in documents if doc["kind"] in {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"})
    assert rbac.subjects({("ServiceAccount", reader["name"], account_namespace)}) == {
        Permission(source_namespace, "", "secrets", "get", source_name)
    }

    private = [
        doc
        for doc in documents
        if doc.get("apiVersion") == "pkg.crossplane.io/v1"
        and doc["kind"] == "Provider"
        and doc["spec"]["package"].startswith("git.allegedly.works/")
    ]
    assert private
    credential = {"name": mirror["target"].get("name", source_name)}
    for provider in private:
        assert credential in provider["spec"].get("packagePullSecrets", []), provider["metadata"]["name"]


def test_litellm_provider_waits_for_crossplane_health_before_exposing_its_resources(
    documents: list[dict[str, Any]],
) -> None:
    kustomization = find_resource(documents, "Kustomization", "crossplane-provider-litellm")
    assert kustomization["spec"]["dependsOn"] == [
        {"name": "crossplane", "namespace": "ducktape-flux"},
        {"name": "external-secrets-operator", "namespace": "ducktape-flux"},
        {"name": "forgejo-images", "namespace": "ducktape-flux"},
    ]
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["prune"] is False
    assert kustomization["spec"]["healthCheckExprs"] == [
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'False')",
        },
        {
            "apiVersion": "pkg.crossplane.io/v1",
            "kind": "Provider",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
        },
    ]


def test_opentofu_workspace_takes_over_forgejo_root_after_tofu_and_preserves_state(
    runfiles: Runfiles, documents: list[dict[str, Any]]
) -> None:
    provider = find_resource(documents, "Provider", "provider-opentofu")
    provider_kustomization = find_resource(documents, "Kustomization", "crossplane-provider-opentofu")
    provider_config = find_resource(documents, "ProviderConfig", "crossplane-forgejo-agentydragon")
    workspace = find_resource(documents, "Workspace", "forgejo-agentydragon")
    resources_kustomization = find_resource(documents, "Kustomization", "crossplane-forgejo-agentydragon")
    assert provider["spec"]["package"].startswith("xpkg.upbound.io/upbound/provider-opentofu:")
    assert provider_kustomization["spec"]["dependsOn"] == [{"name": "crossplane", "namespace": "ducktape-flux"}]
    assert provider_kustomization["spec"]["wait"] is True
    assert provider_kustomization["spec"]["prune"] is False
    assert provider_kustomization["spec"]["healthCheckExprs"] == [
        {
            "apiVersion": "pkg.crossplane.io/v1",
            "kind": "Provider",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
        }
    ]
    assert provider_config["spec"]["configuration"] == 'terraform {\n  backend "pg" {}\n}\n'
    assert 'schema_name = "forgejo_agentydragon"' in provider_config["spec"]["backendFile"]
    assert "PGPASSWORD" not in provider_config["spec"]["backendFile"]
    assert workspace["metadata"]["annotations"]["crossplane.io/external-name"] == "default"
    assert workspace["spec"]["providerConfigRef"] == {"name": "crossplane-forgejo-agentydragon"}
    assert workspace["spec"]["managementPolicies"] == ["Observe", "Create", "Update"]
    assert workspace["spec"]["deletionPolicy"] == "Orphan"
    assert workspace["spec"]["forProvider"]["source"] == "Inline"
    assert "remotePullPolicy" not in workspace["spec"]["forProvider"]
    root = runfiles.root() / "ducktape/tf/gitops/forgejo-agentydragon"
    assert workspace["spec"]["forProvider"]["module"] == "\n".join(
        source.read_text() for source in sorted(root.glob("*.tf"))
    )
    assert workspace["spec"]["forProvider"]["env"] == [
        {
            "name": "PGPASSWORD",
            "secretKeyRef": {"name": "tofu-state-db-credentials", "namespace": "flux-system", "key": "password"},
        }
    ]
    assert resources_kustomization["spec"]["dependsOn"] == [
        {"name": "crossplane-provider-opentofu", "namespace": "ducktape-flux"},
        {"name": "tofu-state-db", "namespace": "ducktape-flux"},
    ]
    assert resources_kustomization["spec"]["wait"] is True
    assert resources_kustomization["spec"]["prune"] is True
    assert not any(
        doc.get("apiVersion") == "infra.contrib.fluxcd.io/v1alpha2"
        and doc.get("kind") == "Terraform"
        and doc.get("metadata", {}).get("name") == "forgejo-agentydragon"
        for doc in documents
    )


def test_litellm_crossplane_keys_retain_publication_after_handoff(documents: list[dict[str, Any]]) -> None:
    kustomizations = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "Kustomization"
        and doc.get("metadata", {}).get("name") in {"litellm-keys-tf", "litellm-crossplane-keys"}
    }

    assert kustomizations["litellm-keys-tf"]["spec"]["dependsOn"] == [{"name": "litellm", "namespace": "ducktape-flux"}]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["dependsOn"] == [
        {"name": "crossplane-provider-litellm", "namespace": "ducktape-flux"},
        {"name": "litellm", "namespace": "ducktape-flux"},
    ]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["wait"] is True

    keys = {
        doc["spec"]["forProvider"]["keyAlias"]: doc
        for doc in documents
        if doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Key"
    }
    assert keys
    assert len(keys) == sum(
        doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Key" for doc in documents
    )
    for alias in ("claude-subscription-clients", "codex-clients", "gemini-clients", "antigravity-clients"):
        assert keys[alias]["spec"]["forProvider"]["keyWoSecretRef"] == {
            "name": f"litellm-client-key-{alias}",
            "key": "api-key",
        }

    published_key_secrets = [
        doc
        for doc in documents
        if doc.get("kind") == "ExternalSecret"
        and any(
            data.get("remoteRef", {}).get("key", "").startswith("litellm-crossplane-key-")
            for data in doc.get("spec", {}).get("data", [])
        )
    ]
    assert published_key_secrets
    published_connections = [
        data["remoteRef"]["key"]
        for doc in published_key_secrets
        for data in doc["spec"]["data"]
        if data["remoteRef"]["key"].startswith("litellm-crossplane-key-")
    ]
    assert len(published_connections) == len(set(published_connections))
    assert set(published_connections) == {
        key["spec"]["writeConnectionSecretToRef"]["name"]
        for key in keys.values()
        if "writeConnectionSecretToRef" in key["spec"]
    }
    assert all(
        data["remoteRef"]["property"] == "attribute.key"
        for doc in published_key_secrets
        for data in doc["spec"]["data"]
        if data["remoteRef"]["key"].startswith("litellm-crossplane-key-")
    )

    teams = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Team"
    }
    assert "routerSettings" not in teams["team-antigravity-clients"]["spec"]["forProvider"]
    assert teams["team-codex-clients"]["spec"]["forProvider"]["routerSettings"]["fallbacks"]
    assert teams["team-gemini-clients"]["spec"]["forProvider"]["routerSettings"]["fallbacks"]


def test_ducktape_artifact_copy_sources_are_in_sparse_checkout(parsed_yaml_documents: YamlDocuments) -> None:
    documents = [doc for docs in parsed_yaml_documents.values() for doc in docs]
    source = find_resource(documents, "GitRepository", "ducktape", namespace="ducktape-flux")
    sparse_roots = tuple(path.strip("/") for path in source["spec"].get("sparseCheckout", []))
    generator = find_resource(documents, "ArtifactGenerator", "ducktape-artifacts", namespace="ducktape-flux")
    repo_aliases = {
        artifact_source["alias"]
        for artifact_source in generator["spec"]["sources"]
        if artifact_source["kind"] == "GitRepository"
        and artifact_source["name"] == "ducktape"
        and artifact_source.get("namespace", "ducktape-flux") == "ducktape-flux"
    }
    assert repo_aliases, "ducktape-artifacts must read the ducktape GitRepository"

    missing: list[str] = []
    for artifact in generator["spec"]["artifacts"]:
        for copy in artifact["copy"]:
            alias, separator, pattern = copy["from"].removeprefix("@").partition("/")
            if alias not in repo_aliases or not separator:
                continue
            literal_parts = []
            for part in pattern.split("/"):
                if any(character in part for character in "*?[{"):
                    break
                literal_parts.append(part)
            required_root = "/".join(literal_parts)
            if not any(required_root == root or required_root.startswith(f"{root}/") for root in sparse_roots):
                missing.append(f"{artifact['name']}: {copy['from']}")

    assert not missing, (
        "ducktape ArtifactGenerator copy sources must be covered by the ducktape GitRepository sparseCheckout:\n"
        + "\n".join(missing)
    )


def test_namespace_readers_can_reconcile_before_the_application(parsed_yaml_documents: YamlDocuments) -> None:
    # Diagnostics must come up even when the application in the target namespace is unhealthy.
    graph = parsed_yaml_documents[Path(HAND_WRITTEN_ROOT, "flux/kustomizations.k8s.yaml")]
    roles = find_resource(graph, "Kustomization", "claude-rbac")["metadata"]
    root = Path(GENERATED_ROOT, "agents/namespace-rbac")
    readers = [
        doc["spec"]
        for doc in graph
        if doc["kind"] == "Kustomization" and Path(doc["spec"].get("path", "")).parent == root
    ]
    assert {Path(spec["path"]) for spec in readers} == {root / namespace for namespace in NAMESPACE_DIAGNOSTICS}
    for spec in readers:
        assert spec["dependsOn"] == [{"name": roles["name"], "namespace": roles["namespace"]}], spec["path"]


def test_flux_kustomizations_under_parked_path_are_annotated(parsed_yaml_documents: YamlDocuments) -> None:
    flux_chart = Path(HAND_WRITTEN_ROOT, "flux/kustomizations.k8s.yaml")
    missing = sorted(
        doc["metadata"]["name"]
        for doc in parsed_yaml_documents[flux_chart]
        if doc.get("kind") == "Kustomization"
        and Path(doc["spec"]["path"].removeprefix("./")).is_relative_to(PARKED_ROOT)
        and doc["metadata"].get("annotations", {}).get("ducktape.org/parked") != "true"
    )
    assert not missing, f"Kustomizations under {PARKED_ROOT}/ lack the parked annotation: {missing}"


def test_goldilocks_explicit_decision(active_yaml_documents: YamlDocuments) -> None:
    update_mode = "goldilocks.fairwinds.com/vpa-update-mode"
    enabled = "goldilocks.fairwinds.com/enabled"
    missing = sorted(
        f"{path}: Namespace '{doc['metadata']['name']}'"
        for path, docs in active_yaml_documents.items()
        for doc in docs
        if doc.get("kind") == "Namespace"
        and update_mode not in (labels := (doc["metadata"].get("labels") or {}))
        and labels.get(enabled) != "false"
    )
    assert not missing, "Generated Namespaces must label their Goldilocks decision:\n" + "\n".join(missing)


def test_haku_ci_vpa_targets_only_egress(documents: list[dict[str, Any]]) -> None:
    namespace = "haku-ci"
    name = "haku-ci-egress"
    ns = find_resource(documents, "Namespace", namespace)
    assert ns["metadata"]["labels"]["goldilocks.fairwinds.com/enabled"] == "false"
    [vpa] = [
        doc for doc in documents if doc["kind"] == "VerticalPodAutoscaler" and doc["metadata"]["namespace"] == namespace
    ]
    assert vpa["metadata"]["name"] == name
    deployment = find_resource(documents, "Deployment", name, namespace=namespace)
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    assert vpa["spec"]["targetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": name}
    assert vpa["spec"]["updatePolicy"] == {"updateMode": "Initial"}
    assert vpa["spec"]["resourcePolicy"]["containerPolicies"] == [
        {
            "containerName": container["name"],
            "controlledResources": ["cpu", "memory"],
            "controlledValues": "RequestsOnly",
        },
        {"containerName": "*", "mode": "Off"},
    ]


def test_claude_session_sync_vpa_opt_in_is_limited_to_deployments(documents: list[dict[str, Any]]) -> None:
    namespace = "claude-session-sync"
    ns = find_resource(documents, "Namespace", namespace)
    labels = ns["metadata"]["labels"]
    mode_key = "goldilocks.fairwinds.com/vpa-update-mode"
    assert labels[mode_key] == "off"
    assert labels.get("goldilocks.fairwinds.com/enabled") != "false"
    resources = [doc for doc in documents if doc.get("metadata", {}).get("namespace") == namespace]
    assert not any(doc["kind"] == "VerticalPodAutoscaler" for doc in resources)
    # Eviction could interrupt the control Pod while it rotates the OAuth credential.
    assert {
        (doc["kind"], doc["metadata"]["name"], annotations[mode_key])
        for doc in resources
        if mode_key in (annotations := doc["metadata"].get("annotations", {}))
    } == {("Deployment", "claude-session-sync", "initial"), ("Deployment", "control", "initial")}


def test_policy_bindings_resolve_sets(documents: list[dict[str, Any]]) -> None:
    policy_sets = {
        (doc["metadata"].get("namespace", ""), doc["metadata"]["name"])
        for doc in documents
        if doc.get("kind") == "PolicySet"
    }
    missing = [
        f"PolicyBinding '{namespace}/{doc['metadata']['name']}' names PolicySet '{policy_set}', which is not rendered in {namespace}"
        for doc in documents
        if doc.get("kind") == "PolicyBinding"
        for namespace in [doc["metadata"].get("namespace", "")]
        for policy_set in doc["spec"].get("policySets", [])
        if (namespace, policy_set) not in policy_sets
    ]
    assert not missing, "\n".join(missing)


@pytest.fixture(scope="module")
def owner_specs(active_api_objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        doc["metadata"]["name"]: doc["spec"]
        for doc in active_api_objects
        if doc["kind"] == "Kustomization" and doc["metadata"].get("namespace") == "ducktape-flux"
    }


def test_parked_haku_backup_and_shared_services_remain_configured(
    generated: Path,
    active_api_objects: list[dict[str, Any]],
    parsed_yaml_documents: YamlDocuments,
    owner_specs: dict[str, dict[str, Any]],
) -> None:
    # Keep Haku's namespace and the public coder's namespace and access Roles alive.
    assert {"haku-namespace", "public-coder-agent-access"} <= owner_specs.keys()
    assert any(
        doc["kind"] == "Namespace" and doc["metadata"]["name"] == "public-coder-agent" for doc in active_api_objects
    )
    # Only the retained backup recovery package remains reproducible outside the live roots.
    archived = Path(PARKED_ROOT, "haku-openclaw-spike")
    backup = parsed_yaml_documents[archived / "backup/backup.k8s.yaml"]
    bucket = next(doc for doc in backup if doc["kind"] == "Bucket")
    assert bucket["spec"]["reclaimPolicy"] == "Retain"
    credentials = next(doc for doc in backup if doc["kind"] == "S3Credentials")
    assert credentials["spec"]["reclaimPolicy"] == "Retain"
    assert (generated / archived / "backup/repository.sops.yaml").is_file()
    assert (generated / archived / "backup/README.md").is_file()


def test_parked_claude_namespace_preserves_credentials_and_egress_fence(
    active_api_objects: list[dict[str, Any]], owner_specs: dict[str, dict[str, Any]]
) -> None:
    # Claude's shared roles, identities and credentials survive; ad-hoc compute does not.
    claude = [doc for doc in active_api_objects if doc["metadata"].get("namespace") == "claude-sandbox"]
    quota = next(doc for doc in claude if doc["kind"] == "ResourceQuota")
    assert str(quota["spec"]["hard"]["pods"]) == "0"
    fence = next(
        doc for doc in claude if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "parked-compute-egress"
    )
    assert fence["spec"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": []}
    assert {"buildbuddy-api-key", "claude-forgejo-credentials"} <= {
        doc["metadata"]["name"] for doc in claude if doc["kind"] == "ExternalSecret"
    }
    assert not any(
        doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == "inject-mitmproxy" for doc in active_api_objects
    )
    assert {"agent-sandbox-controller", "public-coder-agent-access", "claude-sandbox-secrets"} <= owner_specs.keys()


def test_openclaw_cutover_waits_for_agentplane_credentials(owner_specs: dict[str, dict[str, Any]]) -> None:
    app = owner_specs["public-coder-agent-app"]
    assert "agentplane-staging" in {dependency["name"] for dependency in app["dependsOn"]}
    checks = owner_specs["agentplane-staging"]["healthChecks"]
    assert {c["name"] for c in checks if c["kind"] == "ExternalSecret"} >= {
        "public-coder-clickhouse",
        "public-coder-matrix",
        "brave-search",
    }


def test_public_coder_pause_retains_state_after_devbox_retirement(
    parsed_yaml_documents: YamlDocuments, owner_specs: dict[str, dict[str, Any]]
) -> None:
    roots = (Path(PARKED_ROOT, "public-coder-agent"), Path(GENERATED_ROOT, "agents/public-coder-agent"))
    objects = [
        doc
        for path, docs in parsed_yaml_documents.items()
        if path.name.endswith(".k8s.yaml") and any(path.is_relative_to(root) for root in roots)
        for doc in docs
    ]
    deployments = [doc for doc in objects if doc["kind"] == "Deployment"]
    assert {doc["metadata"]["name"] for doc in deployments} == {"public-coder-agent"}
    assert all(doc["spec"]["replicas"] == 0 for doc in deployments)
    assert {doc["metadata"]["name"] for doc in objects if doc["kind"] == "PersistentVolumeClaim"} == {
        "public-coder-agent-state-v2",
        "public-coder-agent-diagnostics",
        "public-coder-agent-sshpiper-recordings",
    }
    assert any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == "public-coder-agent" for doc in objects)
    # VolSync owns its cache PVC; keep the backup source rather than deleting its owner.
    assert any(doc["kind"] == "ReplicationSource" for doc in objects)
    for name in ("public-coder-agent-access", "public-coder-agent-app", "public-coder-agent-backup"):
        assert owner_specs[name]["deletionPolicy"] == "Orphan"
    assert owner_specs["public-coder-agent-app"].get("suspend", False) is True
    assert owner_specs["public-coder-agent-backup"]["suspend"] is True


if __name__ == "__main__":
    pytest_bazel.main()
