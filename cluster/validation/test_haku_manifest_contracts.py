"""Focused contracts over Haku's deployed Kubernetes manifests."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def test_haku_claude_oauth_proxy_isolated_from_general_sandbox(k8s_dir: Path) -> None:
    """Only the dedicated Haku Claude runner receives proxy authority."""
    template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml").read_text())
    template_namespace = template["metadata"]["namespace"]
    console_config = yaml.safe_load((k8s_dir / "haku/console/config.yaml").read_text())
    runtime = console_config["chat_runtimes"]["claude_code"]
    assert runtime["namespace"] == template_namespace

    mounts = template["spec"]["podTemplate"]["spec"]["containers"][0]["volumeMounts"]
    ca_mount = one(mount for mount in mounts if mount["name"] == "egress-proxy-ca")
    assert str(PurePosixPath(runtime["ca_bundle"]).parent) == ca_mount["mountPath"]

    oauth_ingress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/claude-networkpolicy.yaml").read_text())
    peers = oauth_ingress["spec"]["ingress"][0]["from"]
    namespace_by_peer = {
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]: peer for peer in peers
    }
    assert set(namespace_by_peer) == {template_namespace}

    general_egress = (k8s_dir / "agents/haku-egress-proxy/ccnp-haku-proxy-egress.yaml").read_text()
    assert "haku-claude-oauth-proxy" not in general_egress

    claude_egress_path = k8s_dir / "agents/haku-egress-proxy/ccnp-haku-claude-sandbox-egress.yaml"
    claude_egress_text = claude_egress_path.read_text()
    assert template_namespace in claude_egress_text
    assert "haku-claude-oauth-proxy" in claude_egress_text

    service = yaml.safe_load((k8s_dir / "haku/console/service.yaml").read_text())
    bridge_service_port = next(port for port in service["spec"]["ports"] if port["port"] == 9090)
    deployment = yaml.safe_load((k8s_dir / "haku/console/deployment.yaml").read_text())
    server = next(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "server"
    )
    bridge_target_port = next(
        port["containerPort"] for port in server["ports"] if port["name"] == bridge_service_port["targetPort"]
    )
    claude_egress = yaml.safe_load(claude_egress_text)
    console_rule = next(
        rule
        for rule in claude_egress["spec"]["egress"]
        if rule.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("k8s:app.kubernetes.io/name") == "haku-console"
    )
    assert console_rule["toPorts"][0]["ports"] == [{"port": str(bridge_target_port), "protocol": "TCP"}]

    general_injection = (k8s_dir / "kyverno/policies/inject-haku-egress-proxy.yaml").read_text()
    assert "haku-claude-sandbox" not in general_injection

    # The system prompt is read at startup, so a path that names nothing the ConfigMap carries
    # is a pod that never becomes Ready. Tie the three places that must agree — the configured
    # path, the mount point, and the generated file — together here rather than in a rollout.
    kustomization = yaml.safe_load((k8s_dir / "haku/console/kustomization.yaml").read_text())
    generated = next(entry for entry in kustomization["configMapGenerator"] if entry["name"] == "haku-console-config")
    config_mount = next(mount for mount in server["volumeMounts"] if mount["name"] == "config")
    template_path = PurePosixPath(runtime["system_prompt_template"])
    assert str(template_path.parent) == config_mount["mountPath"]
    assert template_path.name in generated["files"]
    assert (k8s_dir / "haku/console" / template_path.name).is_file()

    env_names = {entry["name"] for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert not any(name.startswith("HAKU_CONSOLE_CLAUDE_RUNTIME__") for name in env_names)


def sandbox_env(template: dict[str, object]) -> dict[str, object]:
    container = template["spec"]["podTemplate"]["spec"]["containers"][0]  # type: ignore[index]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_both_sandbox_images_satisfy_the_shared_bootstrap(k8s_dir: Path) -> None:
    """One bootstrap script runs in two images, so each must supply what it hard-requires.

    `haku-sandbox-setup.sh` writes ~/.netrc from `${HAKU_GIT_USERNAME:?}` / `${HAKU_GIT_PASSWORD:?}`
    and aborts the whole claim when either is unset. That is the right behaviour and exactly why
    it wants a test: the failure lands at claim time on whichever image forgot, which for the
    Claude runner means a session that never comes up.
    """
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "the bootstrap declares no required variables — did the ${VAR:?} form change?"

    for name in ("sandboxtemplate-haku.yaml", "sandboxtemplate-haku-claude.yaml"):
        template = yaml.safe_load((k8s_dir / "haku/workspaces/app" / name).read_text())
        assert required <= set(sandbox_env(template)), f"{name} does not satisfy the bootstrap"


def test_claude_sandbox_can_reach_the_forgejo_the_bootstrap_clones_from(k8s_dir: Path) -> None:
    """The clone target and the egress policy that permits it must not drift apart."""
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    url = one(re.findall(r"HAKU_STATE_URL:-http://([a-z0-9-]+)\.([a-z0-9-]+):(\d+)/", script))
    _, namespace, port = url

    egress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/ccnp-haku-claude-sandbox-egress.yaml").read_text())
    allowed = {
        (rule["toEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"], ports["port"])
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule
        for entry in rule.get("toPorts", [])
        for ports in entry["ports"]
    }
    assert (namespace, port) in allowed


def test_both_haku_runtimes_share_one_grant(k8s_dir: Path) -> None:
    """Haku runs on two harnesses, and "what can Haku do to the cluster" must have one answer.

    A ServiceAccount is namespaced, so the identity exists twice; the authority must not. Both
    pods' SAs are subjects on the single haku-sandbox-admin binding, and neither namespace
    grants anything of its own — a second binding would be a second answer, free to drift.
    """
    binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    role = yaml.safe_load((k8s_dir / "haku/rbac/role.yaml").read_text())
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    subjects = {(s["kind"], s["name"], s["namespace"]) for s in binding["subjects"]}

    for template_name, namespace in (
        ("sandboxtemplate-haku.yaml", "haku-sandbox"),
        ("sandboxtemplate-haku-claude.yaml", "haku-claude-sandbox"),
    ):
        spec = yaml.safe_load((k8s_dir / "haku/workspaces/app" / template_name).read_text())
        pod = spec["spec"]["podTemplate"]["spec"]
        assert pod["automountServiceAccountToken"] is True, template_name
        assert ("ServiceAccount", pod["serviceAccountName"], namespace) in subjects, template_name

    # No grant inside the Claude namespace itself: full CRUD there would let a session create
    # further pods behind the subscription-token proxy, which is what its isolation is for.
    claude_ns = k8s_dir / "haku/claude-namespace"
    kinds = {
        yaml.safe_load(path.read_text())["kind"]
        for path in claude_ns.glob("*.yaml")
        if path.name != "kustomization.yaml"
    }
    assert not kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}


def test_haku_console_deployment_version_contract(k8s_dir: Path) -> None:
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
    assert containers["server"]["image"].rsplit(":", 1)[1] == runtime_tags["HAKU_CONSOLE_IMAGE_TAG"]
    assert "HAKU_CONSOLE_STATIC_IMAGE_TAG" not in runtime_tags
    static_container = one(static_deployment["spec"]["template"]["spec"]["containers"])
    static_tag = static_container["image"].rsplit(":", 1)[1]
    static_metadata = yaml.safe_load((console_dir / "static-metadata.yaml").read_text(encoding="utf-8"))
    assert static_metadata["data"]["image-tag"] == static_tag

    static_tag_file = runtime_tags["HAKU_CONSOLE_STATIC_IMAGE_TAG_FILE"]
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


def test_haku_console_migration_release_gate(k8s_dir: Path) -> None:
    """Only the image-coupled, unprivileged Job owns Console DDL in a rollout."""
    console_dir = k8s_dir / "haku" / "console"
    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text(encoding="utf-8"))
    job = yaml.safe_load((console_dir / "migration" / "job.yaml").read_text(encoding="utf-8"))
    service_account = yaml.safe_load((console_dir / "migration" / "serviceaccount.yaml").read_text(encoding="utf-8"))
    migration_flux = yaml.safe_load((console_dir / "migration" / "flux-kustomization.yaml").read_text(encoding="utf-8"))
    console_flux = yaml.safe_load((console_dir / "flux-kustomization.yaml").read_text(encoding="utf-8"))

    server = one(deployment["spec"]["template"]["spec"]["containers"])
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
        "HAKU_CONSOLE_DATABASE_URL",
    }
    assert migration_env["HAKU_CONSOLE_DATABASE_URL"]["value"].startswith("postgresql+asyncpg://")
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
    assert literal_env["HAKU_CONSOLE_PUBLIC_BASE_URL"] == f"https://{one(route['spec']['hostnames'])}"
    assert "HAKU_CONSOLE_MCP_OAUTH__PUBLIC_BASE_URL" not in {entry["name"] for entry in server["env"]}


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
