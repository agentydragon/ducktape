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
        # indexer chunk pod EXCEPT haku-forgejo-git: the colocated egress decide endpoint runs on the
        # API server and must hold the haku Forgejo credential to substitute it into the hosted haku
        # agent's fenced Forgejo egress, so the "API pod holds no index Git credential" boundary is
        # deliberately traded for that agent using its full Forgejo user (read/write/push) through the
        # fence — the write exposure bounded by haku-state `main` branch protection
        # (forgejo_branch_protection: force-push/delete blocked). Every other secret stays unshared.
        # Between chunk and embed exactly the narrow database role is shared.
        # TODO(indexer-forgejo-read-cred): give the haku-state indexer its OWN read-only Forgejo
        # credential (distinct from the shared `haku` write password), then drop this carve-out and
        # restore full server<->chunk secret disjointness — the API server would no longer need to
        # share haku-forgejo-git with the chunk pod.
        forgejo_git_egress_secret = "haku-forgejo-git"
        assert chunk_pod["automountServiceAccountToken"] is False
        assert chunk_env["HAKU_INDEXER__DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == db_secret
        assert _secret_refs(server).isdisjoint(_secret_refs(chunk_container) - {forgejo_git_egress_secret})
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


def test_haku_matrix_adapter_worker_contract(k8s_dir: Path) -> None:
    """The Matrix credential and loop live on the adapter pod; the console API pod carries neither."""
    console_dir = k8s_dir / "haku" / "console"
    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text(encoding="utf-8"))
    adapter_raw = (console_dir / "matrix-adapter-deployment.yaml").read_text(encoding="utf-8")
    adapter = yaml.safe_load(adapter_raw)
    adapter_pod = adapter["spec"]["template"]["spec"]
    adapter_container = one(adapter_pod["containers"])
    server = one(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "server")

    assert adapter_raw.count('# {"$imagepolicy": "flux-system:haku-matrix-adapter"}') == 1
    assert adapter["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert adapter_pod["automountServiceAccountToken"] is False

    # The whole Matrix surface left the console pod: no matrix-shaped env, and the bot-password
    # Secret's only consumer in this namespace is the adapter. The bot password is reflected in
    # from the matrix namespace — the reflection source must allow this namespace and name the
    # Secret the adapter mounts, so a rename on either side fails here rather than at runtime.
    server_env = {entry["name"]: entry for entry in server["env"]}
    assert not any("MATRIX" in name for name in server_env)
    adapter_env = {entry["name"]: entry for entry in adapter_container["env"]}
    password_secret = adapter_env["HAKU_MATRIX_ADAPTER__MATRIX__PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert password_secret["name"] not in _secret_refs(server)
    assert "optional" not in password_secret
    reflection_source = one(
        doc
        for doc in yaml.safe_load_all(
            (k8s_dir / "matrix" / "secrets" / "haku-matrix-bot-password.sops.yaml").read_text(encoding="utf-8")
        )
        if doc.get("kind") == "Secret"
    )
    assert reflection_source["metadata"]["name"] == password_secret["name"]
    reflection_namespaces = reflection_source["metadata"]["annotations"][
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"
    ]
    assert adapter["metadata"]["namespace"] in reflection_namespaces.split(",")

    # The image is a private Forgejo package: the pod's pull secret must be the ducktape-ci
    # credential, whose reflection source must both name that Secret and grant this namespace —
    # and the Flux scan must authenticate the same repository with the same credential.
    pull_secret = one(adapter_pod["imagePullSecrets"])["name"]
    registry_creds = one(
        doc
        for doc in yaml.safe_load_all(
            (k8s_dir / "forgejo-images" / "registry-creds.sops.yaml").read_text(encoding="utf-8")
        )
        if doc.get("kind") == "Secret"
    )
    assert registry_creds["metadata"]["name"] == pull_secret
    for scope in ("allowed", "auto"):
        namespaces = registry_creds["metadata"]["annotations"][
            f"reflector.v1.k8s.emberstack.com/reflection-{scope}-namespaces"
        ]
        assert adapter["metadata"]["namespace"] in namespaces.split(",")
    image_repository = one(
        document
        for document in yaml.safe_load_all(
            (k8s_dir / "flux-image-automation-forgejo" / "haku-matrix-adapter-image.yaml").read_text(encoding="utf-8")
        )
        if document["kind"] == "ImageRepository"
    )
    assert adapter_container["image"].startswith(image_repository["spec"]["image"] + ":")
    assert image_repository["spec"]["secretRef"]["name"] == pull_secret

    # The operator-subject mapping is shared state with the console (one SSOT key), and it is the
    # only Secret the two pods share: the OIDC client secrets in that Secret's other keys stay off
    # this pod, and everything else the adapter mounts is its own.
    oidc_refs = [
        entry["valueFrom"]["secretKeyRef"]
        for entry in adapter_container["env"]
        if "valueFrom" in entry
        and "secretKeyRef" in entry["valueFrom"]
        and entry["valueFrom"]["secretKeyRef"]["name"] in _secret_refs(server)
    ]
    assert {ref["key"] for ref in oidc_refs} == {"operator_subject"}
    subject_secret = one({ref["name"] for ref in oidc_refs})
    assert server_env["HAKU_CONSOLE__OPERATOR_OIDC__CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == (
        subject_secret
    )

    # The adapter resolves that subject through anchor rows written at console login, so the two
    # Deployments must name one trust domain.
    assert (
        adapter_env["HAKU_MATRIX_ADAPTER__OPERATOR_IDENTITY_TRUST_DOMAIN"]["value"]
        == server_env["HAKU_CONSOLE__OPERATOR_IDENTITY__TRUST_DOMAIN"]["value"]
    )

    # The launch-identity registry is the one deploy-owned config file the console reads: the
    # shared ConfigMap, mounted at the path the worker's config-file setting names.
    config_volume = one(volume for volume in adapter_pod["volumes"] if volume["name"] == "config")
    server_config_volume = one(
        volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "config"
    )
    assert config_volume["configMap"]["name"] == server_config_volume["configMap"]["name"]
    config_mount = one(mount for mount in adapter_container["volumeMounts"] if mount["name"] == "config")
    assert adapter_env["HAKU_MATRIX_ADAPTER_CONFIG_FILE"]["value"] == f"{config_mount['mountPath']}/config.yaml"

    # Reloader watches exactly what the pod mounts.
    annotations = adapter["metadata"]["annotations"]
    assert annotations["configmap.reloader.stakater.com/reload"] == config_volume["configMap"]["name"]
    assert set(annotations["secret.reloader.stakater.com/reload"].split(",")) == _secret_refs(adapter_container)

    # The narrow database role, wired end to end: the Deployment consumes the ESO-generated
    # Secret, CNPG syncs that Secret's password onto the managed role of the same name, and the
    # provisioner SQL grants to that role — and never to it via a default-privileges blanket.
    db_secret = adapter_env["HAKU_MATRIX_ADAPTER__DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
    role_secret_docs = list(
        yaml.safe_load_all((console_dir / "db" / "matrix-adapter-role-secret.yaml").read_text(encoding="utf-8"))
    )
    external_secret = one(doc for doc in role_secret_docs if doc["kind"] == "ExternalSecret")
    assert external_secret["spec"]["target"]["name"] == db_secret
    cluster_cr = yaml.safe_load((console_dir / "db" / "postgres-cluster.yaml").read_text(encoding="utf-8"))
    role = one(role for role in cluster_cr["spec"]["managed"]["roles"] if role["passwordSecret"]["name"] == db_secret)
    assert external_secret["spec"]["target"]["template"]["data"]["username"] == role["name"]
    sql = (console_dir / "matrix-adapter-role.sql").read_text(encoding="utf-8")
    assert f"TO {role['name']}" in sql
    assert "ALTER DEFAULT PRIVILEGES" not in sql


if __name__ == "__main__":
    pytest_bazel.main()
