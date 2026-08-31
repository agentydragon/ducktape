"""Contracts for Haku Console rollout, edge, and migration wiring."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one


def test_haku_console_rollout_and_service_contract(k8s_dir: Path) -> None:
    """API and static releases are independently safe and report their actual images."""
    console_dir = k8s_dir / "haku" / "console"
    deployment_path = console_dir / "deployment.yaml"
    static_deployment_path = console_dir / "static-deployment.yaml"
    raw = deployment_path.read_text(encoding="utf-8")
    static_raw = static_deployment_path.read_text(encoding="utf-8")
    deployment = yaml.safe_load(raw)
    static_deployment = yaml.safe_load(static_raw)

    # `maxUnavailable: 0` is the property worth pinning: a replacement that never becomes Ready
    # leaves the previous version serving. Recreate did the opposite — every pod deleted before one
    # started — which turned a two-minute missing Secret into a full console outage on 2026-08-10.
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    containers = {container["name"]: container for container in deployment["spec"]["template"]["spec"]["containers"]}
    runtime_tags = {entry["name"]: entry["value"] for entry in containers["server"]["env"] if "value" in entry}
    assert containers["server"]["image"].rsplit(":", 1)[1] == runtime_tags["HAKU_CONSOLE__IMAGE_TAG"]
    assert "HAKU_CONSOLE__STATIC_IMAGE_TAG" not in runtime_tags
    static_container = one(static_deployment["spec"]["template"]["spec"]["containers"])
    static_tag = static_container["image"].rsplit(":", 1)[1]
    static_metadata = yaml.safe_load((console_dir / "static-metadata.yaml").read_text(encoding="utf-8"))
    assert static_metadata["data"]["image-tag"] == static_tag

    static_tag_file = runtime_tags["HAKU_CONSOLE__STATIC_IMAGE_TAG_FILE"]
    static_metadata_mount = next(
        mount for mount in containers["server"]["volumeMounts"] if mount["name"] == "static-metadata"
    )
    assert static_tag_file == f"{static_metadata_mount['mountPath']}/image-tag"
    static_metadata_volume = next(
        volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "static-metadata"
    )
    assert static_metadata_volume["configMap"]["name"] == static_metadata["metadata"]["name"]

    api_service = yaml.safe_load((console_dir / "service.yaml").read_text(encoding="utf-8"))
    static_service = yaml.safe_load((console_dir / "static-service.yaml").read_text(encoding="utf-8"))
    assert api_service["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    assert (
        static_service["spec"]["selector"].items()
        <= static_deployment["spec"]["template"]["metadata"]["labels"].items()
    )
    static_port = one(static_service["spec"]["ports"])
    assert static_port["targetPort"] in {port["name"] for port in static_container["ports"]}
    route = yaml.safe_load((console_dir / "httproute.yaml").read_text(encoding="utf-8"))
    route_backend = one(route["spec"]["rules"][0]["backendRefs"])
    assert (route_backend["name"], route_backend["port"]) == (static_service["metadata"]["name"], static_port["port"])
    assert static_deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    static_env = {entry["name"]: entry["value"] for entry in static_container["env"] if "value" in entry}
    upstream_host, upstream_port_text = static_env["HAKU_CONSOLE_API_UPSTREAM"].rsplit(":", 1)
    assert upstream_host == (
        f"{api_service['metadata']['name']}.{api_service['metadata']['namespace']}.svc.cluster.local"
    )
    api_port = one(port for port in api_service["spec"]["ports"] if port["port"] == int(upstream_port_text))
    assert api_port["targetPort"] in {port["name"] for port in containers["server"]["ports"]}

    annotations = deployment["metadata"]["annotations"]
    assert "reloader.stakater.com/auto" not in annotations
    config_volume = one(
        volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "config"
    )
    assert annotations["configmap.reloader.stakater.com/reload"] == config_volume["configMap"]["name"]
    reloaded_secrets = set(annotations["secret.reloader.stakater.com/reload"].split(","))
    referenced_secrets = {
        entry["valueFrom"]["secretKeyRef"]["name"]
        for entry in containers["server"]["env"]
        if "valueFrom" in entry and "secretKeyRef" in entry["valueFrom"]
    }
    assert reloaded_secrets == referenced_secrets

    all_image_policy_text = (
        raw
        + static_raw
        + (console_dir / "static-metadata.yaml").read_text(encoding="utf-8")
        + (console_dir / "migration" / "job.yaml").read_text(encoding="utf-8")
    )
    for marker, expected_count in (
        ('# {"$imagepolicy": "flux-system:haku-console"}', 2),
        ('# {"$imagepolicy": "flux-system:haku-console:tag"}', 1),
        ('# {"$imagepolicy": "flux-system:haku-console-static"}', 1),
        ('# {"$imagepolicy": "flux-system:haku-console-static:tag"}', 1),
    ):
        assert all_image_policy_text.count(marker) == expected_count, f"missing or duplicated Flux marker: {marker}"


def test_haku_console_runtime_observer_rbac_contract(k8s_dir: Path) -> None:
    """The active-session observer has only namespaced, read-only graph observation access."""
    workspaces_dir = k8s_dir / "haku" / "workspaces" / "app"
    role = yaml.safe_load((workspaces_dir / "haku-console-runtime-claim-role.yaml").read_text(encoding="utf-8"))
    binding = yaml.safe_load(
        (workspaces_dir / "haku-console-runtime-claim-rolebinding.yaml").read_text(encoding="utf-8")
    )

    assert role["metadata"] == {"name": "haku-console-runtime-claims", "namespace": "haku-runtime-sandbox"}
    permissions = {(tuple(rule["apiGroups"]), tuple(rule["resources"])): set(rule["verbs"]) for rule in role["rules"]}
    assert permissions == {
        (("extensions.agents.x-k8s.io",), ("sandboxclaims",)): {"create", "delete", "get", "list", "patch", "watch"},
        (("agents.x-k8s.io",), ("sandboxes",)): {"get", "list", "watch"},
        (("",), ("pods",)): {"get", "list", "watch"},
    }
    assert binding["metadata"] == {"name": "haku-console-runtime-claims", "namespace": "haku-runtime-sandbox"}
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "haku-console-runtime-claims",
    }
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "haku-console", "namespace": "haku-console"}]


def test_haku_console_migration_release_gate(k8s_dir: Path) -> None:
    """Only the image-coupled, unprivileged Job owns Console DDL in a rollout."""
    console_dir = k8s_dir / "haku" / "console"
    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text(encoding="utf-8"))
    job = yaml.safe_load((console_dir / "migration" / "job.yaml").read_text(encoding="utf-8"))
    service_account = yaml.safe_load((console_dir / "migration" / "serviceaccount.yaml").read_text(encoding="utf-8"))
    migration_flux = yaml.safe_load((console_dir / "migration" / "flux-kustomization.yaml").read_text(encoding="utf-8"))
    console_flux = yaml.safe_load((console_dir / "flux-kustomization.yaml").read_text(encoding="utf-8"))

    server = one(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "server"
    )
    migrate = one(job["spec"]["template"]["spec"]["containers"])
    assert job["metadata"]["name"] == "haku-console-migration"
    assert job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "enabled"
    assert "ttlSecondsAfterFinished" not in job["spec"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert job["spec"]["template"]["spec"]["enableServiceLinks"] is False
    assert service_account["automountServiceAccountToken"] is False
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == service_account["metadata"]["name"]
    assert (
        job["spec"]["template"]["spec"]["serviceAccountName"]
        != deployment["spec"]["template"]["spec"]["serviceAccountName"]
    )
    assert migrate["args"] == ["migrate"]
    assert migrate["image"] == server["image"]
    assert migrate["imagePullPolicy"] == "Always"
    assert migrate["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert job["spec"]["template"]["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    migration_env = {entry["name"]: entry for entry in migrate["env"]}
    assert set(migration_env) == {
        "HAKU_CONSOLE_DB_USER",
        "HAKU_CONSOLE_DB_PASSWORD",
        "HAKU_CONSOLE_DB_HOST",
        "HAKU_CONSOLE_DB_PORT",
        "HAKU_CONSOLE_DB_NAME",
        "HAKU_CONSOLE__DATABASE_URL",
    }
    assert migration_env["HAKU_CONSOLE__DATABASE_URL"]["value"].startswith("postgresql+asyncpg://")
    assert {entry["valueFrom"]["secretKeyRef"]["name"] for entry in migration_env.values() if "valueFrom" in entry} == {
        "haku-console-db-app"
    }
    assert migration_flux["spec"]["wait"] is True
    assert migration_flux["spec"]["healthChecks"] == [
        {"apiVersion": "batch/v1", "kind": "Job", "name": "haku-console-migration", "namespace": "haku-console"}
    ]
    assert {entry["name"] for entry in migration_flux["spec"]["dependsOn"]} == {"haku-console-db"}
    assert "haku-console-migration" in {entry["name"] for entry in console_flux["spec"]["dependsOn"]}
    root_kustomization = yaml.safe_load((k8s_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "haku/console/migration/flux-kustomization.yaml" in root_kustomization["resources"]


def test_haku_console_oauth_edge_contract(k8s_dir: Path) -> None:
    """Haku serves only on TLS, preserves one canonical origin, and emits HSTS at the edge."""
    route = yaml.safe_load((k8s_dir / "haku" / "console" / "httproute.yaml").read_text(encoding="utf-8"))
    assert route["spec"]["parentRefs"] == [
        {"name": "cluster-gateway", "namespace": "gateway-system", "sectionName": "https-wildcard"}
    ]
    assert route["spec"]["rules"][0]["filters"] == [
        {
            "type": "ResponseHeaderModifier",
            "responseHeaderModifier": {"set": [{"name": "Strict-Transport-Security", "value": "max-age=31536000"}]},
        }
    ]

    deployment = yaml.safe_load((k8s_dir / "haku" / "console" / "deployment.yaml").read_text(encoding="utf-8"))
    server = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "server")
    literal_env = {entry["name"]: entry["value"] for entry in server["env"] if "value" in entry}
    assert literal_env["HAKU_CONSOLE__PUBLIC_BASE_URL"] == f"https://{one(route['spec']['hostnames'])}"
    assert "HAKU_CONSOLE__MCP_OAUTH__PUBLIC_BASE_URL" not in {entry["name"] for entry in server["env"]}


def test_haku_ci_keda_resources_are_wired_to_the_runner_job(k8s_dir: Path) -> None:
    keda_repository, keda_release = list(
        yaml.safe_load_all((k8s_dir / "keda/helmrelease.yaml").read_text(encoding="utf-8"))
    )
    auth, scaled_job = list(yaml.safe_load_all((k8s_dir / "haku-ci/scaledjob.yaml").read_text(encoding="utf-8")))
    source_ref = keda_release["spec"]["chart"]["spec"]["sourceRef"]
    assert (source_ref["name"], source_ref["namespace"]) == (
        keda_repository["metadata"]["name"],
        keda_repository["metadata"]["namespace"],
    )
    assert keda_release["spec"]["values"]["watchNamespace"] == scaled_job["metadata"]["namespace"]

    token_manifest = yaml.safe_load((k8s_dir / "haku/managed-agent/haku-forgejo-tea.sops.yaml").read_text())
    [secret_ref] = auth["spec"]["secretTargetRef"]
    assert secret_ref["name"] == token_manifest["metadata"]["name"]
    assert secret_ref["key"] in token_manifest["stringData"]

    [trigger] = scaled_job["spec"]["triggers"]
    assert trigger["authenticationRef"]["name"] == auth["metadata"]["name"]

    annotations = token_manifest["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == auth["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == auth["metadata"]["namespace"]


if __name__ == "__main__":
    pytest_bazel.main()
