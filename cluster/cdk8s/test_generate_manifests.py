"""Pinning tests for the cdk8s manifest generators.

The generated-output snapshot (STYLE.md § Testing) over every file the generator writes:
the committed files are the source of truth, and regeneration must reproduce them exactly.
`GENERATED_ROOT` is closed in the other direction too, so a hand-added or stale file there
fails; generated files beside hand-written ones under `HAND_WRITTEN_ROOT` or `PARKED_ROOT`
are pinned only one way.
"""

from difflib import unified_diff
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml

from cluster.cdk8s.generate_manifests import generate_manifests
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT, PARKED_ROOT
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.undeclared_outputs import undeclared_outputs_dir

_REGENERATE = "regenerate with `bb run //cluster/cdk8s:generate_manifests` and commit the result"


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory, checkout: Path) -> Path:
    root = tmp_path_factory.mktemp("generated")
    generate_manifests(root)
    # CI uses the same pinned generator as local runs. Preserve its exact drift as an
    # applyable artifact rather than making authors reconstruct generated YAML by hand.
    patch: list[str] = []
    for relative in sorted(_files(root)):
        committed = checkout / relative
        before = committed.read_text().splitlines(keepends=True) if committed.is_file() else []
        after = (root / relative).read_text().splitlines(keepends=True)
        patch.extend(
            unified_diff(
                before, after, fromfile=f"a/{relative}" if committed.is_file() else "/dev/null", tofile=f"b/{relative}"
            )
        )
    if patch:
        (undeclared_outputs_dir() / "generated-manifests.patch").write_text("".join(patch))
    return root


@pytest.fixture(scope="module")
def parsed_yaml_documents(generated: Path) -> dict[Path, tuple[Any, ...]]:
    """Cache parsed documents for read-only inspection by manifest assertions."""
    return {path: tuple(yaml.safe_load_all(path.read_text())) for path in generated.rglob("*.yaml")}


def _generated_documents(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Objects written by cdk8s, including generated files beside hand-written inputs."""
    generated_root = generated / GENERATED_ROOT
    hand_written_root = generated / HAND_WRITTEN_ROOT
    return [
        doc
        for path, documents in parsed_yaml_documents.items()
        if path.is_relative_to(generated_root)
        or (path.is_relative_to(hand_written_root) and path.name.endswith(".k8s.yaml"))
        for doc in documents
        if isinstance(doc, dict)
    ]


@pytest.fixture(scope="module")
def checkout() -> Path:
    """The runfiles tree, which holds each committed file the data deps package at its repo path."""
    return get_required_path(f"_main/{GENERATED_ROOT}").parents[1]


def test_generated_tree_matches_committed_snapshot(generated: Path, checkout: Path) -> None:
    generated_files = _files(generated)
    committed_files = _files(checkout / GENERATED_ROOT)
    missing = sorted(relative for relative in generated_files if not (checkout / relative).is_file())
    stale = sorted(
        relative
        for relative in generated_files
        if (checkout / relative).is_file() and (generated / relative).read_text() != (checkout / relative).read_text()
    )
    committed = {f"{GENERATED_ROOT}/{relative}" for relative in committed_files}
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


def test_piraeus_storage_is_confined_to_prepared_nodes(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    release = next(
        doc
        for doc in documents
        if doc.get("kind") == "HelmRelease" and doc["metadata"].get("name") == "linstor-cluster"
    )
    values = release["spec"]["values"]
    controller_selector = {"storage.allegedly.works/piraeus-ready": "true", "topology.kubernetes.io/region": "hil"}

    assert values["createInternalTLS"] == "cert-manager"
    assert values["createApiTLS"] == "cert-manager"
    cluster = values["linstorCluster"]
    assert "nodeSelector" not in cluster
    assert cluster["nodeAffinity"] == {
        "nodeSelectorTerms": [
            {
                "matchExpressions": [
                    {"key": "storage.allegedly.works/piraeus-ready", "operator": "In", "values": ["true"]}
                ]
            }
        ]
    }
    for component in ("controller", "csiController", "affinityController"):
        assert cluster[component]["podTemplate"]["spec"]["nodeSelector"] == controller_selector
    assert cluster["properties"] == [{"name": "TcpPortAutoRange", "value": "7900-7999"}]
    assert cluster["highAvailabilityController"] == {"enabled": True}
    assert cluster["nfsServer"] == {"enabled": False}

    (
        ssd_satellite,
        next_ssd_satellite,
        third_ssd_satellite,
        optiplex_satellite,
        hdd_satellite,
        _next_hdd_satellite,
        _last_hdd_satellite,
        wyrm2_satellite,
        rugged_satellite,
    ) = values["linstorSatelliteConfigurations"]
    assert ssd_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "ovh-ns1001419",
    }
    assert ssd_satellite["deletionPolicy"] == "Retain"
    [reactor_patch] = ssd_satellite["patches"]
    assert reactor_patch["target"] == {"version": "v1", "kind": "ConfigMap", "name": "reactor-config"}
    assert 'address = "127.0.0.1:9942"' in reactor_patch["patch"]
    assert "satellite-config" not in reactor_patch["patch"]
    assert "bind_address" not in reactor_patch["patch"]
    assert "3366" not in reactor_patch["patch"]

    assert next_ssd_satellite["name"] == "ssd-ovh-ns104952"
    assert next_ssd_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "ovh-ns104952",
    }
    assert next_ssd_satellite["deletionPolicy"] == "Retain"
    assert next_ssd_satellite["podTemplate"] == ssd_satellite["podTemplate"]
    assert next_ssd_satellite["patches"] == ssd_satellite["patches"]
    [next_ssd_pool] = next_ssd_satellite["storagePools"]
    assert next_ssd_pool == {
        "name": "ssd",
        "lvmThinPool": {
            "volumeGroup": "piraeus_ssd",
            "thinPool": "data",
            "logicalVolumeCreateArguments": ["--poolmetadatasize", "4G", "--chunksize", "64K"],
        },
        "source": {"hostDevices": ["/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8304019P450RGN"]},
    }

    assert third_ssd_satellite["name"] == "ssd-ovh-ns104963"
    assert third_ssd_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "ovh-ns104963",
    }
    assert third_ssd_satellite["deletionPolicy"] == "Retain"
    assert third_ssd_satellite["podTemplate"] == ssd_satellite["podTemplate"]
    assert third_ssd_satellite["patches"] == ssd_satellite["patches"]
    [third_ssd_pool] = third_ssd_satellite["storagePools"]
    assert third_ssd_pool == {
        "name": "ssd",
        "lvmThinPool": {
            "volumeGroup": "piraeus_ssd",
            "thinPool": "data",
            "logicalVolumeCreateArguments": ["--poolmetadatasize", "4G", "--chunksize", "64K"],
        },
        "source": {"hostDevices": ["/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8256009U450RGN"]},
    }

    assert hdd_satellite["name"] == "hdd-ovh-ns103656"
    assert hdd_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "ovh-ns103656",
    }
    assert hdd_satellite["deletionPolicy"] == "Retain"
    assert hdd_satellite["podTemplate"] == optiplex_satellite["podTemplate"] == ssd_satellite["podTemplate"]
    assert hdd_satellite["patches"] == ssd_satellite["patches"]
    [hdd_pool] = hdd_satellite["storagePools"]
    assert hdd_pool == {
        "name": "hdd",
        "lvmThinPool": {
            "volumeGroup": "piraeus_hdd",
            "thinPool": "data",
            "logicalVolumeCreateArguments": ["--poolmetadatasize", "4G", "--chunksize", "64K"],
        },
        "source": {"hostDevices": ["/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H8UMLA"]},
    }

    # Wyrm2 offers both local classes' pools, laid out like every other node's.
    assert wyrm2_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "wyrm2",
    }
    wyrm2_ssd_pool, wyrm2_hdd_pool = wyrm2_satellite["storagePools"]
    assert wyrm2_ssd_pool["lvmThinPool"] == ssd_satellite["storagePools"][0]["lvmThinPool"]
    assert wyrm2_ssd_pool["name"] == "ssd"
    assert wyrm2_hdd_pool["lvmThinPool"] == hdd_satellite["storagePools"][0]["lvmThinPool"]
    assert wyrm2_hdd_pool["name"] == "hdd"
    assert wyrm2_satellite["properties"] == hdd_satellite["properties"]
    assert wyrm2_satellite["patches"] == ssd_satellite["patches"]

    # Rugged's `ssd` pool is laid out like every other node's, on the NixOS satellite template.
    assert rugged_satellite["nodeSelector"] == {
        "storage.allegedly.works/piraeus-ready": "true",
        "kubernetes.io/hostname": "rugged",
    }
    [rugged_pool] = rugged_satellite["storagePools"]
    assert rugged_pool["name"] == "ssd"
    assert rugged_pool["lvmThinPool"] == ssd_satellite["storagePools"][0]["lvmThinPool"]
    assert rugged_satellite["podTemplate"] == wyrm2_satellite["podTemplate"]

    ssd_class, hdd_class, replicated_ssd_class = values["storageClasses"]
    assert ssd_class == {
        "name": "piraeus-ssd-local",
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
        "parameters": {
            "linstor.csi.linbit.com/storagePool": "ssd",
            "linstor.csi.linbit.com/placementCount": "1",
            "linstor.csi.linbit.com/allowRemoteVolumeAccess": "false",
        },
    }
    assert hdd_class == {
        **ssd_class,
        "name": "piraeus-hdd-local",
        "parameters": {**ssd_class["parameters"], "linstor.csi.linbit.com/storagePool": "hdd"},
    }
    # Replicated copies and remote attachments both stay inside the OVH site: never over the
    # home/OVH link.
    assert replicated_ssd_class["name"] == "piraeus-ssd-2-autodiskful"
    replicated_parameters = replicated_ssd_class["parameters"]
    assert replicated_parameters["linstor.csi.linbit.com/placementCount"] == "2"
    assert replicated_parameters["linstor.csi.linbit.com/replicasOnSame"] == "topology.kubernetes.io/zone=hil-ovh"
    assert yaml.safe_load(replicated_parameters["linstor.csi.linbit.com/allowRemoteVolumeAccess"]) == [
        {"fromSame": ["topology.kubernetes.io/zone"]}
    ]

    operator_flux = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "piraeus-operator"
    )
    cluster_flux = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "piraeus-cluster"
    )
    assert operator_flux["spec"]["prune"] is False
    assert operator_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["prune"] is False
    assert cluster_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["dependsOn"] == [
        {"name": "piraeus-operator", "namespace": "ducktape-flux"},
        {"name": "cert-manager", "namespace": "ducktape-flux"},
        {"name": "monitoring-crds", "namespace": "ducktape-flux"},
    ]


def test_crossplane_core_waits_for_its_controller_and_retains_its_inventory(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    release = next(
        doc for doc in documents if doc.get("kind") == "HelmRelease" and doc["metadata"].get("name") == "crossplane"
    )
    kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane"
    )

    assert release["metadata"]["namespace"] == "crossplane-system"
    assert kustomization["spec"]["dependsOn"] == [{"name": "kyverno", "namespace": "ducktape-flux"}]
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["prune"] is False


def test_litellm_provider_waits_for_crossplane_health_before_exposing_its_resources(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    provider = next(
        doc for doc in documents if doc.get("kind") == "Provider" and doc["metadata"].get("name") == "provider-litellm"
    )
    pull_secret = next(
        doc
        for doc in documents
        if doc.get("kind") == "ExternalSecret"
        and doc["metadata"].get("name") == "forgejo-images-creds"
        and doc["metadata"].get("namespace") == "crossplane-system"
    )
    kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane-provider-litellm"
    )

    assert provider["apiVersion"] == "pkg.crossplane.io/v1"
    assert provider["spec"]["revisionActivationPolicy"] == "Automatic"
    assert provider["spec"]["package"].startswith("git.allegedly.works/ducktape-ci/provider-litellm:")
    assert provider["spec"]["packagePullSecrets"] == [{"name": "forgejo-images-creds"}]
    assert pull_secret["apiVersion"] == "external-secrets.io/v1"
    assert pull_secret["spec"]["secretStoreRef"] == {
        "kind": "ClusterSecretStore",
        "name": "kubernetes-crossplane-forgejo-images-creds-secret-store",
    }
    assert pull_secret["spec"]["target"]["template"]["type"] == "kubernetes.io/dockerconfigjson"
    store = next(
        doc
        for doc in documents
        if doc.get("kind") == "ClusterSecretStore"
        and doc["metadata"].get("name") == "kubernetes-crossplane-forgejo-images-creds-secret-store"
    )
    assert store["spec"]["conditions"] == [{"namespaces": ["crossplane-system"]}]
    assert store["spec"]["provider"]["kubernetes"]["remoteNamespace"] == "forgejo-images"
    assert store["spec"]["provider"]["kubernetes"]["auth"]["serviceAccount"] == {
        "name": "crossplane-forgejo-images-creds-reader"
    }
    reader_role = next(
        doc
        for doc in documents
        if doc.get("kind") == "Role" and doc["metadata"].get("name") == "crossplane-forgejo-images-creds-reader"
    )
    assert reader_role["metadata"]["namespace"] == "forgejo-images"
    assert reader_role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["forgejo-images-creds"], "verbs": ["get"]}
    ]
    reader_binding = next(
        doc
        for doc in documents
        if doc.get("kind") == "RoleBinding" and doc["metadata"].get("name") == "crossplane-forgejo-images-creds-reader"
    )
    assert reader_binding["subjects"] == [
        {
            "apiGroup": "",
            "kind": "ServiceAccount",
            "name": "crossplane-forgejo-images-creds-reader",
            "namespace": "crossplane-system",
        }
    ]
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
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    provider = next(
        doc for doc in documents if doc.get("kind") == "Provider" and doc["metadata"].get("name") == "provider-opentofu"
    )
    provider_kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane-provider-opentofu"
    )
    provider_config = next(
        doc
        for doc in documents
        if doc.get("apiVersion") == "opentofu.upbound.io/v1beta1"
        and doc.get("kind") == "ProviderConfig"
        and doc.get("metadata", {}).get("name") == "crossplane-forgejo-agentydragon"
    )
    workspace = next(
        doc
        for doc in documents
        if doc.get("apiVersion") == "opentofu.upbound.io/v1beta1"
        and doc.get("kind") == "Workspace"
        and doc.get("metadata", {}).get("name") == "forgejo-agentydragon"
    )
    resources_kustomization = next(
        doc
        for doc in documents
        if doc.get("kind", "").lower() == "kustomization"
        and doc.get("metadata", {}).get("name") == "crossplane-forgejo-agentydragon"
    )
    assert provider["spec"]["package"] == "xpkg.upbound.io/upbound/provider-opentofu:v1.1.10"
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
    assert "Delete" not in workspace["spec"]["managementPolicies"]
    assert workspace["spec"]["deletionPolicy"] == "Orphan"
    assert workspace["spec"]["forProvider"]["source"] == "Inline"
    assert "remotePullPolicy" not in workspace["spec"]["forProvider"]
    root = get_required_path(own_repo_rlocation("tf/gitops/forgejo-agentydragon"))
    assert workspace["spec"]["forProvider"]["module"] == "\n".join(
        source.read_text() for source in sorted(root.glob("*.tf"))
    )
    assert workspace["spec"]["forProvider"]["planArgs"] == ["-lock-timeout=5m"]
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


def test_litellm_crossplane_keys_retain_publication_after_handoff(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    kustomizations = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "Kustomization"
        and doc.get("metadata", {}).get("name")
        in {"litellm-keys-tf", "litellm-crossplane-keys", "litellm-buildbuddy-sync"}
    }

    assert kustomizations["litellm-keys-tf"]["spec"]["dependsOn"] == [
        {"name": "tofu-controller", "namespace": "ducktape-flux"},
        {"name": "litellm", "namespace": "ducktape-flux"},
    ]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["dependsOn"] == [
        {"name": "crossplane-provider-litellm", "namespace": "ducktape-flux"},
        {"name": "litellm", "namespace": "ducktape-flux"},
    ]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["wait"] is True
    assert kustomizations["litellm-buildbuddy-sync"]["spec"]["dependsOn"] == [
        {"name": "tofu-controller", "namespace": "ducktape-flux"},
        {"name": "litellm-crossplane-keys", "namespace": "ducktape-flux"},
    ]

    keys = {
        doc["spec"]["forProvider"]["keyAlias"]: doc
        for doc in documents
        if doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Key"
    }
    assert set(keys) == {
        "cheap-experiments",
        "buildbuddy-live-tests",
        "agentplane-staging",
        "codex-pod",
        "public-coder-agent",
        "codex-pod-messages",
        "agent-workspaces-codex",
        "claude-subscription-clients",
        "codex-clients",
        "gemini-clients",
        "antigravity-clients",
    }
    assert not any(
        doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Organization"
        for doc in documents
    )
    for alias in ("claude-subscription-clients", "codex-clients", "gemini-clients", "antigravity-clients"):
        assert keys[alias]["spec"]["forProvider"]["keyWoSecretRef"] == {
            "name": f"litellm-client-key-{alias}",
            "key": "api-key",
        }

    assert all("organizationIdRef" not in key["spec"]["forProvider"] for key in keys.values())

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


def test_litellm_buildbuddy_terraform_runners_receive_api_key_inputs(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    terraform_resources = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "Terraform"
        and doc.get("metadata", {}).get("name") in {"litellm-keys", "litellm-buildbuddy-sync"}
    }
    assert set(terraform_resources) == {"litellm-buildbuddy-sync"}

    expected_env = {
        "name": "BUILDBUDDY_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "buildbuddy-org-admin-api-key", "key": "api-key"}},
    }
    expected_var = {
        "name": "buildbuddy_api_key",
        "valueFrom": {"secretKeyRef": {"name": "buildbuddy-org-admin-api-key", "key": "api-key"}},
    }
    for resource in terraform_resources.values():
        env = resource["spec"]["runnerPodTemplate"]["spec"]["env"]
        assert expected_env in env
        assert expected_var in resource["spec"]["vars"]
        assert not any(variable["name"].startswith("TF_VAR_") for variable in env)


def test_piraeus_controller_network_policy_only_admits_api_tls(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    policy = next(
        document
        for document in _generated_documents(generated, parsed_yaml_documents)
        if document.get("kind") == "NetworkPolicy" and document["metadata"].get("name") == "linstor-controller-api"
    )

    assert policy["metadata"]["namespace"] == "piraeus-datastore"
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "linstor-controller"}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [{"ports": [{"port": 3371, "protocol": "TCP"}]}]


def test_ducktape_artifact_copy_sources_are_in_sparse_checkout(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = [
        document
        for path in generated.rglob("*.yaml")
        for document in parsed_yaml_documents[path]
        if isinstance(document, dict)
    ]
    source = next(
        document
        for document in documents
        if document["kind"] == "GitRepository"
        and document["metadata"].get("name") == "ducktape"
        and document["metadata"].get("namespace") == "ducktape-flux"
    )
    sparse_roots = tuple(path.strip("/") for path in source["spec"].get("sparseCheckout", []))
    generator = next(
        document
        for document in documents
        if document["kind"] == "ArtifactGenerator"
        and document["metadata"].get("name") == "ducktape-artifacts"
        and document["metadata"].get("namespace") == "ducktape-flux"
    )
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


def test_session_archive_credentials_source_reaches_its_secret_generator(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]], checkout: Path
) -> None:
    credentials_source = "secrets/shared/session-backup.yaml"
    assert (checkout / credentials_source).is_file(), "the encrypted credentials source must be in the source checkout"

    documents = _generated_documents(generated, parsed_yaml_documents)
    generator = next(
        document
        for document in documents
        if document.get("kind") == "ArtifactGenerator" and document["metadata"].get("name") == "ducktape-artifacts"
    )
    archive_artifact = next(item for item in generator["spec"]["artifacts"] if item["name"] == "ai-session-archive")
    copies = archive_artifact["copy"]
    assert {(copy["from"], copy["to"]) for copy in copies} >= {
        ("@repo/cluster/generated/ai-session-archive/**", "@artifact/cluster/generated/ai-session-archive/"),
        (
            "@repo/secrets/shared/session-backup.yaml",
            "@artifact/cluster/generated/ai-session-archive/session-backup.sops.yaml",
        ),
    }

    kustomization = yaml.safe_load((generated / "cluster/generated/ai-session-archive/kustomization.yaml").read_text())
    assert kustomization["secretGenerator"] == [
        {
            "name": "session-backup-importer-credentials",
            "namespace": "session-backup",
            "files": ["session-backup.yaml=session-backup.sops.yaml"],
        }
    ]


def test_no_image_automation_markers(generated: Path) -> None:
    # cdk8s can't emit YAML comments, so this should be unreachable -- but if it ever did,
    # Flux's image-automation bot would silently fight the generator for ownership of the
    # file (cluster/docs/cdk8s.md).
    marked = sorted(relative for relative in _files(generated) if "$imagepolicy" in (generated / relative).read_text())
    assert not marked, "Generated files must not carry a Flux image-automation marker:\n" + "\n".join(marked)


def test_flux_kustomizations_under_parked_path_are_annotated(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    flux_chart = generated / HAND_WRITTEN_ROOT / "flux/kustomizations.k8s.yaml"
    missing = sorted(
        doc["metadata"]["name"]
        for doc in parsed_yaml_documents[flux_chart]
        if isinstance(doc, dict)
        and doc.get("kind") == "Kustomization"
        and Path(doc["spec"]["path"].removeprefix("./")).is_relative_to(PARKED_ROOT)
        and doc["metadata"].get("annotations", {}).get("ducktape.org/parked") != "true"
    )
    assert not missing, f"Kustomizations under {PARKED_ROOT}/ lack the parked annotation: {missing}"


def test_goldilocks_explicit_decision(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    update_mode = "goldilocks.fairwinds.com/vpa-update-mode"
    enabled = "goldilocks.fairwinds.com/enabled"
    missing = sorted(
        f"{path.relative_to(generated)}: Namespace '{doc['metadata']['name']}'"
        for path, documents in parsed_yaml_documents.items()
        if path.is_relative_to(generated / GENERATED_ROOT)
        or (path.is_relative_to(generated / HAND_WRITTEN_ROOT) and path.name.endswith(".k8s.yaml"))
        for doc in documents
        if isinstance(doc, dict)
        and doc.get("kind") == "Namespace"
        and update_mode not in (labels := (doc["metadata"].get("labels") or {}))
        and labels.get(enabled) != "false"
    )
    assert not missing, "Generated Namespaces must label their Goldilocks decision:\n" + "\n".join(missing)


def test_policy_bindings_resolve_sets(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
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


def test_cluster_diagnostics_kustomizations_are_read_only(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    role = next(
        doc
        for doc in _generated_documents(generated, parsed_yaml_documents)
        if doc.get("kind") == "ClusterRole" and doc["metadata"]["name"] == "cluster-diagnostics-reader"
    )
    assert "aggregationRule" not in role

    def grants(group: str, resource: str, verb: str) -> bool:
        return any(
            not rule.get("resourceNames")
            and (group in rule.get("apiGroups", []) or "*" in rule.get("apiGroups", []))
            and (resource in rule.get("resources", []) or "*" in rule.get("resources", []))
            and (verb in rule.get("verbs", []) or "*" in rule.get("verbs", []))
            for rule in role["rules"]
        )

    reads = ("get", "list", "watch")
    writes = ("create", "update", "patch", "delete", "deletecollection")
    assert all(grants("kustomize.toolkit.fluxcd.io", "kustomizations", verb) for verb in reads)
    assert not any(grants("kustomize.toolkit.fluxcd.io", "kustomizations", verb) for verb in writes)


def test_haku_spike_is_retired_and_backup_recovery_survives(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    active = [
        doc
        for path in generated.rglob("*.k8s.yaml")
        if "parked" not in path.parts
        for doc in parsed_yaml_documents[path]
        if doc
    ]
    spike = "haku-openclaw-spike"
    assert not any(doc["metadata"].get("namespace") == spike for doc in active)
    assert not any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == spike for doc in active)
    assert not any(doc["metadata"]["name"] == f"{spike}-proxy" for doc in active)
    owners = {doc["metadata"]["name"]: doc for doc in active if doc["kind"] == "Kustomization"}
    assert f"{spike}-backup" not in owners
    assert f"{spike}-app" not in owners
    assert not (generated / "cluster/generated/retired/haku-openclaw-spike").exists()
    tenant_grant = next(
        doc
        for doc in active
        if doc["kind"] == "ResourceReferenceGrant"
        and doc["metadata"].get("namespace") == "seaweedfs"
        and doc["metadata"]["name"] == "tenants"
    )
    for source in tenant_grant["spec"]["from"]:
        for expression in source["namespaceSelector"]["matchExpressions"]:
            assert spike not in expression.get("values", [])
    # Keep Haku sandbox/CI's shared proxy and the public coder's namespace and access Roles alive.
    assert {"haku-egress-proxy", "haku-namespace", "public-coder-agent-access"} <= owners.keys()
    deployments = {
        (doc["metadata"].get("namespace"), doc["metadata"]["name"]) for doc in active if doc["kind"] == "Deployment"
    }
    assert ("haku-egress-proxy", "haku-egress-proxy") in deployments
    assert any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == "public-coder-agent" for doc in active)
    # Only the retained backup recovery package remains reproducible outside the live roots.
    archived = generated / "cluster/parked/haku-openclaw-spike"
    assert not (archived / "app").exists()
    assert not (archived / "app-image-pins").exists()
    backup = parsed_yaml_documents[archived / "backup/backup.k8s.yaml"]
    bucket = next(doc for doc in backup if doc["kind"] == "Bucket")
    assert bucket["spec"]["reclaimPolicy"] == "Retain"
    credentials = next(doc for doc in backup if doc["kind"] == "S3Credentials")
    assert credentials["spec"]["reclaimPolicy"] == "Retain"
    assert (archived / "backup/repository.sops.yaml").is_file()
    assert (archived / "backup/README.md").is_file()


def test_retired_mitmproxy_stays_unwired(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    active = [
        doc
        for path in generated.rglob("*.k8s.yaml")
        if "parked" not in path.parts
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict)
    ]
    retired_namespaces = {"agents-mitmproxy"}
    assert not any(doc["metadata"].get("namespace") in retired_namespaces for doc in active)
    assert not any(doc["kind"] == "Namespace" and doc["metadata"]["name"] in retired_namespaces for doc in active)
    owners = {doc["metadata"]["name"]: doc for doc in active if doc["kind"] == "Kustomization"}
    assert "agents-mitmproxy" not in owners
    for directory in retired_namespaces:
        assert not (generated / "cluster/generated/retired" / directory).exists()
    # Claude's shared roles, identities and credentials survive; ad-hoc compute does not.
    claude = [doc for doc in active if doc["metadata"].get("namespace") == "claude-sandbox"]
    quota = next(doc for doc in claude if doc["kind"] == "ResourceQuota")
    assert str(quota["spec"]["hard"]["pods"]) == "0"
    fence = next(
        doc for doc in claude if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "parked-compute-egress"
    )
    assert fence["spec"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": []}
    assert {"buildbuddy-api-key", "claude-forgejo-credentials"} <= {
        doc["metadata"]["name"] for doc in claude if doc["kind"] == "ExternalSecret"
    }
    assert not any(doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == "inject-mitmproxy" for doc in active)
    assert any(
        doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == "inject-haku-egress-proxy" for doc in active
    )
    assert {
        "agent-sandbox-controller",
        "haku-egress-proxy",
        "public-coder-agent-access",
        "claude-sandbox-secrets",
    } <= owners.keys()


def test_openclaw_cutover_waits_for_agentplane_credentials(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    owners = {
        doc["metadata"]["name"]: doc
        for path in generated.rglob("*.k8s.yaml")
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict) and doc["kind"] == "Kustomization"
    }
    app = owners["public-coder-agent-app"]["spec"]
    assert "agentplane-staging" in {dependency["name"] for dependency in app["dependsOn"]}
    assert app["deletionPolicy"] == "Orphan"
    checks = owners["agentplane-staging"]["spec"]["healthChecks"]
    assert {c["name"] for c in checks if c["kind"] == "ExternalSecret"} >= {
        "public-coder-clickhouse",
        "public-coder-matrix",
        "brave-search",
    }


def test_public_coder_pause_retains_state_after_devbox_retirement(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    roots = (generated / "cluster/parked/public-coder-agent", generated / "cluster/generated/agents/public-coder-agent")
    objects = [
        doc
        for path, docs in parsed_yaml_documents.items()
        if path.name.endswith(".k8s.yaml") and any(path.is_relative_to(root) for root in roots)
        for doc in docs
        if isinstance(doc, dict)
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
    owners = {
        doc["metadata"]["name"]: doc["spec"]
        for docs in parsed_yaml_documents.values()
        for doc in docs
        if isinstance(doc, dict) and doc.get("kind") == "Kustomization" and "spec" in doc
    }
    for name in ("public-coder-agent-access", "public-coder-agent-app", "public-coder-agent-backup"):
        assert owners[name]["deletionPolicy"] == "Orphan"
    assert owners["public-coder-agent-app"].get("suspend", False) is True
    assert owners["public-coder-agent-backup"]["suspend"] is True


if __name__ == "__main__":
    pytest_bazel.main()
