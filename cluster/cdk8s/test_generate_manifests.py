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
from util.bazel.runfiles import get_required_path
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


def test_every_generated_file_is_committed(generated: Path, checkout: Path) -> None:
    missing = sorted(relative for relative in _files(generated) if not (checkout / relative).is_file())
    assert not missing, f"Generated but not committed ({_REGENERATE}):\n" + "\n".join(missing)


def test_generated_files_match_committed(generated: Path, checkout: Path) -> None:
    stale = sorted(
        relative
        for relative in _files(generated)
        if (checkout / relative).is_file() and (generated / relative).read_text() != (checkout / relative).read_text()
    )
    assert not stale, f"Stale ({_REGENERATE}):\n" + "\n".join(stale)


def test_generated_root_holds_only_generated_files(generated: Path, checkout: Path) -> None:
    committed = {f"{GENERATED_ROOT}/{relative}" for relative in _files(checkout / GENERATED_ROOT)}
    extra = sorted(committed - _files(generated))
    assert not extra, (
        f"Committed under {GENERATED_ROOT} but not written by the generator; delete it, or keep a "
        "directory holding a hand-written file whole under the hand-written root:\n" + "\n".join(extra)
    )


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


def test_haku_spike_stays_unwired(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
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
    # Preserved snapshots remain reproducible outside the live manifest roots.
    archived = generated / "cluster/parked/haku-openclaw-spike"
    app = parsed_yaml_documents[archived / "app/app.k8s.yaml"]
    assert any(doc["kind"] == "PersistentVolumeClaim" for doc in app)
    bucket = next(doc for doc in app if doc["kind"] == "Bucket")
    assert bucket["spec"]["reclaimPolicy"] == "Retain"
    assert (archived / "backup/backup.k8s.yaml").is_file()


def test_legacy_sandboxes_stay_unwired(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    active = [
        doc
        for path in generated.rglob("*.k8s.yaml")
        if "parked" not in path.parts
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict)
    ]
    retired_namespaces = {"agents-mitmproxy", "agent-workspaces"}
    assert not any(doc["metadata"].get("namespace") in retired_namespaces for doc in active)
    assert not any(doc["kind"] == "Namespace" and doc["metadata"]["name"] in retired_namespaces for doc in active)
    owners = {doc["metadata"]["name"]: doc for doc in active if doc["kind"] == "Kustomization"}
    assert "agents-mitmproxy" not in owners
    assert "agent-workspaces-app" not in owners
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
    archived = [
        doc
        for path in (generated / "cluster/parked/agent-workspaces").glob("*.k8s.yaml")
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict)
    ]
    assert any(doc["kind"] == "SandboxWarmPool" and doc["spec"]["replicas"] == 1 for doc in archived)
    assert not any(doc["kind"] == "ImagePolicy" and doc["metadata"]["name"] == "agent-workspace" for doc in active)


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
        "public-coder-haku-console",
        "public-coder-clickhouse",
        "public-coder-matrix",
        "brave-search",
    }


def test_public_coder_pause_retains_storage_without_running_agents(
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
    assert {doc["metadata"]["name"] for doc in deployments} == {"public-coder-agent", "proxy", "sshpiper"}
    assert all(doc["spec"]["replicas"] == 0 for doc in deployments)
    vms = [doc for doc in objects if doc["kind"] == "VirtualMachine"]
    assert len(vms) == 1
    assert vms[0]["spec"]["runStrategy"] == "Halted"
    assert "ducktape.org/auto-restart-template-changes" not in vms[0]["metadata"].get("annotations", {})
    assert {doc["metadata"]["name"] for doc in objects if doc["kind"] == "PersistentVolumeClaim"} == {
        "public-coder-agent-state-v2",
        "public-coder-agent-diagnostics",
        "public-coder-agent-sshpiper-recordings",
        "public-coder-devbox-bazel-cache",
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
    for name in ("public-coder-agent-access", "public-coder-agent-app", "public-coder-agent-devbox"):
        assert owners[name]["deletionPolicy"] == "Orphan"
    assert all(owners[f"public-coder-agent-{part}"]["suspend"] for part in ("app", "backup", "devbox"))
    assert owners["public-coder-agent-devbox"]["wait"] is False


if __name__ == "__main__":
    pytest_bazel.main()
