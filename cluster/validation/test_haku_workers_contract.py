"""Contracts for Haku Console worker deployments and their narrow credentials."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one


def _secret_refs(container: dict[str, Any]) -> set[str]:
    return {
        entry["valueFrom"]["secretKeyRef"]["name"]
        for entry in container["env"]
        if "valueFrom" in entry and "secretKeyRef" in entry["valueFrom"]
    }


def test_haku_indexer_worker_contract(k8s_dir: Path) -> None:
    """The indexer roles share the console's registry and vector space but none of its authority.

    The chunk role is one Deployment per logical index (#4886), and the expectations are derived
    from the deploy-owned `recall_indexes` registry rather than a fixed roster: every registry
    index must have exactly one chunk pod, mounting only its own index's config slice and carrying
    only that index's credential — so a new registry index without a Deployment (or a Deployment
    for an unregistered index), and any drift between a slice and its registry entry, fails here.
    """
    console_dir = k8s_dir / "haku" / "console"
    config = yaml.safe_load((console_dir / "config.yaml").read_text(encoding="utf-8"))
    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text(encoding="utf-8"))
    server = one(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "server")
    server_env = {entry["name"]: entry for entry in server["env"]}
    embed_raw = (console_dir / "indexer-embed-deployment.yaml").read_text(encoding="utf-8")
    embed = yaml.safe_load(embed_raw)
    embed_pod = embed["spec"]["template"]["spec"]
    embed_container = one(embed_pod["containers"])
    embed_env = {entry["name"]: entry for entry in embed_container["env"]}
    db_secret = embed_env["HAKU_INDEXER__DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]

    # Search joins `content_embeddings` on the model key the embed role writes, so reader and writer
    # must name the same model. (The endpoint address may legitimately differ; the model may not.)
    assert server_env["HAKU_CONSOLE__EMBEDDER__MODEL"]["value"] == embed_env["HAKU_INDEXER__EMBEDDER__MODEL"]["value"]

    kustomization = yaml.safe_load((console_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    generator_files = {entry["name"]: entry["files"] for entry in kustomization["configMapGenerator"]}
    index_by_id = {index["index_id"]: (slot, index) for slot, index in config["recall_indexes"].items()}
    chunk_index_ids: set[str] = set()
    for path in sorted(console_dir.glob("indexer-chunk-*-deployment.yaml")):
        chunk_raw = path.read_text(encoding="utf-8")
        chunk = yaml.safe_load(chunk_raw)
        chunk_pod = chunk["spec"]["template"]["spec"]
        chunk_container = one(chunk_pod["containers"])
        chunk_env = {entry["name"]: entry for entry in chunk_container["env"]}

        # Each pod is keyed by the one index its mounted config slice defines — the same authority
        # the running pod reads (there is no selector env; the slice IS the selection). The chain
        # pod volume -> generated ConfigMap -> slice file must resolve, and the naming convention
        # ties Deployment, ConfigMap, and slice file to the index.
        config_volume = one(volume for volume in chunk_pod["volumes"] if volume["name"] == "config")
        configmap_name = config_volume["configMap"]["name"]
        slice_key, _, slice_name = one(generator_files[configmap_name]).partition("=")
        slice_config = yaml.safe_load((console_dir / slice_name).read_text(encoding="utf-8"))
        slice_slot, slice_index = one(slice_config["recall_indexes"].items())
        index_id = slice_index["index_id"]
        assert index_id in index_by_id, f"{path.name} slices an unregistered index {index_id!r}"
        registry_slot, registry_index = index_by_id[index_id]
        assert slice_slot == registry_slot
        chunk_index_ids.add(index_id)
        assert path.name == f"indexer-chunk-{index_id}-deployment.yaml", path.name
        assert chunk["metadata"]["name"] == f"haku-indexer-chunk-{index_id}"
        assert configmap_name == f"haku-indexer-chunk-{index_id}-config"
        assert slice_name == f"indexer-chunk-{index_id}-config.yaml"

        # The slice is exactly the registry projection: this index's entry verbatim plus the Git CA
        # bundle the console reads, and nothing else — so a console-only or another index's config
        # change (or parse breakage) can never reach this pod. The config-file setting names the
        # mounted slice.
        assert slice_config == {
            "git_ca_bundle": config["git_ca_bundle"],
            "recall_indexes": {registry_slot: registry_index},
        }
        config_mount = one(mount for mount in chunk_container["volumeMounts"] if mount["name"] == "config")
        assert chunk_env["HAKU_INDEXER_CONFIG_FILE"]["value"] == f"{config_mount['mountPath']}/{slice_key}"

        # One binary, one role flag, the one Flux policy rewriting the same image as embed. A
        # replacement that cannot start (schema-incompatible image) crash-loops while the previous
        # replica keeps maintaining the index.
        assert chunk_container["args"] == ["--role=chunk"]
        assert chunk_container["image"] == embed_container["image"]
        assert chunk_raw.count('# {"$imagepolicy": "flux-system:haku-indexer"}') == 1
        assert chunk["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0

        # Narrow identity: no ServiceAccount token. The console API pod shares no secret with the
        # indexer chunk pod — in particular no Git credential; only the haku-state chunk pod holds
        # `haku-forgejo-git`. Between chunk and embed exactly the narrow database role is shared.
        assert chunk_pod["automountServiceAccountToken"] is False
        assert chunk_env["HAKU_INDEXER__DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == db_secret
        assert _secret_refs(server).isdisjoint(_secret_refs(chunk_container))
        assert _secret_refs(chunk_container) & _secret_refs(embed_container) == {db_secret}

        # Credential minimization by index: a chunk pod overlays the typed Git credential leaves
        # only for the private Forgejo source. The pod's env is exactly its settings contract, {config_file,
        # database_url} plus its own Git slots — in particular no embedder endpoint and no index
        # selector — and its secret set is exactly its DB role plus its own Git slots.
        credential_prefix = f"HAKU_INDEXER__RECALL_INDEXES__{registry_slot.upper()}__CREDENTIALS__"
        git_slots = {f"{credential_prefix}USERNAME", f"{credential_prefix}PASSWORD"}
        if not registry_index.get("repo_url", "").startswith("http://forgejo-http."):
            git_slots = set()
        for var in git_slots:
            assert "secretKeyRef" in chunk_env[var]["valueFrom"], f"registry slot {var} unbound on {index_id}"
        assert set(chunk_env) == {"HAKU_INDEXER_CONFIG_FILE", "HAKU_INDEXER__DATABASE_URL"} | git_slots
        git_secrets = {chunk_env[var]["valueFrom"]["secretKeyRef"]["name"] for var in git_slots}
        assert _secret_refs(chunk_container) == {db_secret} | git_secrets

        # Reloader watches exactly what each pod mounts.
        chunk_annotations = chunk["metadata"]["annotations"]
        assert chunk_annotations["configmap.reloader.stakater.com/reload"] == configmap_name
        assert set(chunk_annotations["secret.reloader.stakater.com/reload"].split(",")) == _secret_refs(chunk_container)

    # The chunk Deployments equal the registry both ways: a registry index with no chunk pod, or a
    # chunk pod for an unregistered index, fails.
    assert chunk_index_ids == set(index_by_id)

    # The embed role works off the database queue alone: exactly the shared DB role, no index Git
    # credential, no registry, and nothing else mounted either.
    assert embed_container["args"] == ["--role=embed"]
    assert embed_raw.count('# {"$imagepolicy": "flux-system:haku-indexer"}') == 1
    assert embed["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert embed_pod["automountServiceAccountToken"] is False
    assert _secret_refs(server).isdisjoint(_secret_refs(embed_container))
    assert _secret_refs(embed_container) == {db_secret}
    assert "HAKU_INDEXER_CONFIG_FILE" not in embed_env
    assert "volumes" not in embed_pod
    embed_annotations = embed["metadata"]["annotations"]
    assert "configmap.reloader.stakater.com/reload" not in embed_annotations
    assert set(embed_annotations["secret.reloader.stakater.com/reload"].split(",")) == _secret_refs(embed_container)

    # The narrow database role, wired end to end: every Deployment consumes the ESO-generated
    # Secret, CNPG syncs that Secret's password onto the managed role of the same name, and the
    # provisioner SQL grants to that role.
    role_secret_docs = list(
        yaml.safe_load_all((console_dir / "db" / "indexer-role-secret.yaml").read_text(encoding="utf-8"))
    )
    external_secret = one(doc for doc in role_secret_docs if doc["kind"] == "ExternalSecret")
    assert external_secret["spec"]["target"]["name"] == db_secret
    cluster_cr = yaml.safe_load((console_dir / "db" / "postgres-cluster.yaml").read_text(encoding="utf-8"))
    role = one(role for role in cluster_cr["spec"]["managed"]["roles"] if role["passwordSecret"]["name"] == db_secret)
    assert external_secret["spec"]["target"]["template"]["data"]["username"] == role["name"]
    sql = (console_dir / "indexer-role.sql").read_text(encoding="utf-8")
    assert f"TO {role['name']}" in sql


if __name__ == "__main__":
    pytest_bazel.main()
