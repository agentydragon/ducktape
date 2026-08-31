"""Focused contracts over Haku's deployed Kubernetes manifests."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, cast

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
    runtime = console_config["harnesses"]["claude_code"]
    assert runtime["namespace"] == template_namespace == "haku-runtime-sandbox"
    assert runtime["agent_id"] == "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2"
    assert runtime["claim_prefix"] == "claude"
    assert runtime["harness_label"] == "claude"
    # Claude Code's inference runs against the in-cluster LiteLLM gateway (-> CLIProxyAPI), never
    # api.anthropic.com. Tie the runner's gateway origin and auth placeholder to the fence entries
    # that admit and substitute them — the whole of #4670 — rather than restating the model roster
    # (the model + haiku_model slugs are pinned against the served anthropic-max20/ant-messages/* lane in
    # cluster/k8s/litellm/app/test_litellm_config.py).
    implementation = runtime["implementation"]
    assert implementation["kind"] == "claude_code"
    assert implementation["api_base_url"].startswith("http://")
    assert "anthropic.com" not in implementation["api_base_url"]
    egress = console_config["egress_decide"]
    litellm_grant = one(grant for grant in egress["grants"] if grant["id"] == "haku-claude-litellm")
    origin = one(litellm_grant["origins"])
    assert implementation["api_base_url"] == f"{origin['scheme']}://{origin['host']}:{origin['port']}"
    # LiteLLM resolves to a ClusterIP inside prohibited_cidrs, so the gateway is reachable only
    # because this entry lifts the private-address denial (#5073).
    assert litellm_grant["allow_prohibited_address"] is True
    credential = one(c for c in egress["credentials"] if c["handle"] == litellm_grant["credential_handle"])
    assert implementation["auth_token_placeholder"] == credential["placeholder"]
    assert implementation["model"]
    assert implementation["haiku_model"]

    # ActivityWatch follows the same placeholder-substitution path, but its credential is
    # deliberately read-only: the runner gets only the inert env value, the Console registry
    # binds the reflected Secret to Haku's Agent, and POST is pinned to the query endpoint.
    runner_environment = sandbox_env(template)
    assert runner_environment["AW_READ_TOKEN"] == {
        "name": "AW_READ_TOKEN",
        "value": "activitywatch-read-token-placeholder",
    }
    activity_credential = one(c for c in egress["credentials"] if c["handle"] == "activitywatch-read-token")
    assert activity_credential["placeholder"] == runner_environment["AW_READ_TOKEN"]["value"]
    assert activity_credential["value_env_var"] == "HAKU_EGRESS_CREDENTIAL_ACTIVITYWATCH_READ"
    assert activity_credential["origins"] == [
        {"scheme": "https", "host": "activitywatch-read.allegedly.works", "port": 443}
    ]
    activity_grants = [grant for grant in egress["grants"] if grant["id"].startswith("haku-activitywatch-")]
    assert {tuple(grant["coverage"]["methods"]) for grant in activity_grants} == {("GET",), ("POST",)}
    query_grant = one(grant for grant in activity_grants if grant["id"] == "haku-activitywatch-query")
    assert query_grant["coverage"]["path_regex"] == "/api/0/query/.*"
    assert all(grant["credential_handle"] == activity_credential["handle"] for grant in activity_grants)

    read_token = yaml.safe_load((k8s_dir / "x/activitywatch/activitywatch-read-token.sops.yaml").read_text())
    annotations = read_token["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"].split(",") == [
        "haku-egress-proxy",
        "haku-console",
    ]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"].split(",") == [
        "haku-egress-proxy",
        "haku-console",
    ]
    assert "mcp_static_agent_id" not in runtime
    assert "oauth_placeholder" not in runtime
    pod_template = template["spec"]["podTemplate"]
    assert pod_template["metadata"]["labels"]["haku.allegedly.works/access-profile-id"] == "haku"
    assert pod_template["spec"]["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod_template["spec"]

    mounts = pod_template["spec"]["containers"][0]["volumeMounts"]
    ca_mount = one(mount for mount in mounts if mount["name"] == "egress-proxy-ca")
    assert str(PurePosixPath(runtime["ca_bundle"]).parent) == ca_mount["mountPath"]

    oauth_ingress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/claude-networkpolicy.yaml").read_text())
    peers = oauth_ingress["spec"]["ingress"][0]["from"]
    namespace_by_peer = {
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]: peer for peer in peers
    }
    assert set(namespace_by_peer) == {template_namespace}
    assert namespace_by_peer[template_namespace]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "haku-harness-runner",
        "haku.allegedly.works/access-profile-id": "haku",
    }

    general_egress = (k8s_dir / "agents/haku-egress-proxy/ccnp-haku-proxy-egress.yaml").read_text()
    assert "haku-claude-oauth-proxy" not in general_egress

    agent_egress_path = k8s_dir / "agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml"
    agent_egress_text = agent_egress_path.read_text()
    assert template_namespace in agent_egress_text
    assert "haku.allegedly.works/access-profile-id: haku" in agent_egress_text
    assert "haku-claude-oauth-proxy" in agent_egress_text
    assert "kube-apiserver" not in agent_egress_text

    service = yaml.safe_load((k8s_dir / "haku/console/service.yaml").read_text())
    runner_protocol_service_port = next(port for port in service["spec"]["ports"] if port["port"] == 9090)
    deployment = yaml.safe_load((k8s_dir / "haku/console/deployment.yaml").read_text())
    server = next(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "server"
    )
    runner_protocol_target_port = next(
        port["containerPort"] for port in server["ports"] if port["name"] == runner_protocol_service_port["targetPort"]
    )
    agent_egress = yaml.safe_load(agent_egress_text)
    console_rules = [
        rule
        for rule in agent_egress["spec"]["egress"]
        if rule.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("k8s:app.kubernetes.io/name") == "haku-console"
    ]
    console_ports = {port["port"] for rule in console_rules for port in rule["toPorts"][0]["ports"]}
    # Two rules select the shared Console pod label: the runner protocol (9090 Service -> the
    # server's own port) and the colocated egress fence sidecar's listener (8888, #4670), which the
    # runner's HTTPS_PROXY now points at for both inference and GitHub.
    assert console_ports == {str(runner_protocol_target_port), "8888"}

    kube_proxy_rule = next(
        rule
        for rule in agent_egress["spec"]["egress"]
        if rule.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("k8s:app.kubernetes.io/name")
        == "haku-kube-api-proxy"
    )
    # The proxy's TLS listener: client-go attaches kubeconfig credentials only to an https server.
    assert kube_proxy_rule["toPorts"][0]["ports"] == [{"port": "8443", "protocol": "TCP"}]

    kube_proxy_objects = list(yaml.safe_load_all((k8s_dir / "haku/console/kube-api-proxy.yaml").read_text()))
    kube_proxy_policy = one(
        obj
        for obj in kube_proxy_objects
        if obj["kind"] == "CiliumNetworkPolicy" and obj["metadata"]["name"] == "haku-kube-api-proxy"
    )
    exec_template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku.yaml").read_text())
    exec_pod_labels = exec_template["spec"]["podTemplate"]["metadata"]["labels"]
    kubeconfig_ingress = one(rule for rule in kube_proxy_policy["spec"]["ingress"] if "fromEndpoints" in rule)
    assert {frozenset(peer["matchLabels"].items()) for peer in kubeconfig_ingress["fromEndpoints"]} == {
        frozenset(
            {
                "k8s:io.kubernetes.pod.namespace": template_namespace,
                "k8s:app.kubernetes.io/name": "haku-harness-runner",
                "k8s:haku.allegedly.works/access-profile-id": profile,
            }.items()
        )
        for profile in ("haku", "public-coder")
    } | {
        # The exec pool mounts no ServiceAccount token, so this admission is its only kubectl path.
        frozenset(
            {
                "k8s:io.kubernetes.pod.namespace": exec_template["metadata"]["namespace"],
                "k8s:app.kubernetes.io/name": exec_pod_labels["app.kubernetes.io/name"],
            }.items()
        )
    }
    assert kubeconfig_ingress["toPorts"][0]["ports"] == [{"port": "8443", "protocol": "TCP"}]

    haku_binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    assert haku_binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
        {"kind": "Group", "name": "haku:access-profile:haku", "apiGroup": "rbac.authorization.k8s.io"},
    ]

    general_injection = (k8s_dir / "kyverno/policies/inject-haku-egress-proxy.yaml").read_text()
    assert "haku-claude-sandbox" not in general_injection

    # The prompt templates are read at startup, so a path that names nothing the ConfigMap carries
    # is a pod that never becomes Ready. Tie the three places that must agree — the configured
    # path, the mount point, and the generated file — together here rather than in a rollout.
    # Prompts belong to launchable Agents plus the shared chat fragment (#4431 stage 6).
    kustomization = yaml.safe_load((k8s_dir / "haku/console/kustomization.yaml").read_text())
    generated = next(entry for entry in kustomization["configMapGenerator"] if entry["name"] == "haku-console-config")
    config_mount = next(mount for mount in server["volumeMounts"] if mount["name"] == "config")
    for entry in console_config["launchable_agents"]:
        template_path = PurePosixPath(entry["system_prompt_template"])
        assert str(template_path.parent) == config_mount["mountPath"]
        assert template_path.name in generated["files"]
        template = k8s_dir / "haku/console" / template_path.name
        assert template.is_file()
        # `{% include %}` resolves against the template's own directory, so every included name
        # must ride in the same generated ConfigMap.
        for included in re.findall(r'{%\s*include\s+"([^"]+)"\s*%}', template.read_text()):
            assert included in generated["files"]
            assert (k8s_dir / "haku/console" / included).is_file()

    env_names = {entry["name"] for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert not any(name.startswith("HAKU_CONSOLE_CLAUDE_RUNTIME__") for name in env_names)


def test_public_coder_pod_joins_the_colocated_egress_fence(k8s_dir: Path) -> None:
    """The #4943 spike target: public-coder's OpenClaw pod is wired into the colocated fence.

    The pod carries the fleet `inject-haku-egress-proxy` policy's own CA wiring — same volume
    name, same configMap, same mountPath — which is what every rule's precondition checks for
    (the skip itself is pinned in kyverno/test_proxy_injection.py), so a widening of the fleet
    injection to this namespace skips the pod instead of appending port-8080 env over its iron
    values. This pins the relations that make the spike sound: the carried wiring really is the
    policy's own, the trust Bundle actually delivers that ConfigMap to the pod's namespace (else
    the pod wedges in ContainerCreating), the pod's egress NetworkPolicy admits the listener the
    Service publishes, and the Deployment does NOT cut its default proxy env over to that listener.
    This legacy pod has no live Console bridge bearer, so it remains on its iron proxy; only a
    Console-launched sandbox with a live bridge can use the colocated listener. The migrated Haku
    runner's configuration grants remain coherent with the shared GitHub placeholder.
    """
    deployment = yaml.safe_load((k8s_dir / "agents/public-coder-agent/app/deployment.yaml").read_text())
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    env = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}

    policy = yaml.safe_load((k8s_dir / "kyverno/policies/inject-haku-egress-proxy.yaml").read_text())
    rules = {rule["name"]: rule for rule in policy["spec"]["rules"]}
    injected_volume = one(yaml.safe_load(rules["add-proxy-volume"]["mutate"]["patchesJson6902"]))["value"]
    container_foreach = one(rules["add-proxy-env-and-mount-containers"]["mutate"]["foreach"])
    patches = yaml.safe_load(container_foreach["patchesJson6902"])
    injected_mount = one(p["value"] for p in patches if p["path"].endswith("/volumeMounts/-"))

    pod_volume = one(v for v in pod["volumes"] if v["name"] == injected_volume["name"])
    assert pod_volume["configMap"]["name"] == injected_volume["configMap"]["name"]
    pod_mount = one(m for m in container["volumeMounts"] if m["name"] == injected_volume["name"])
    assert pod_mount["mountPath"] == injected_mount["mountPath"]
    assert f"name=='{injected_volume['name']}'" in one(container_foreach["preconditions"]["all"])["key"]

    bundle = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/trust-bundle.yaml").read_text())
    assert bundle["metadata"]["name"] == injected_volume["configMap"]["name"]
    selector = one(bundle["spec"]["target"]["namespaceSelector"]["matchExpressions"])
    assert deployment["metadata"]["namespace"] in selector["values"]

    service = yaml.safe_load((k8s_dir / "haku/console/egress-proxy-service.yaml").read_text())
    port = one(service["spec"]["ports"])
    listener = (
        f"http://{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local:{port['port']}"
    )
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] != listener

    netpol = yaml.safe_load((k8s_dir / "agents/public-coder-agent/app/networkpolicy-egress.yaml").read_text())
    fence_rule = one(
        rule
        for rule in netpol["spec"]["egress"]
        if any(
            peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
            == service["metadata"]["namespace"]
            for peer in rule["to"]
        )
    )
    peer = one(fence_rule["to"])
    # The sidecar shares the Console pod's network namespace, so the Service's own pod selector
    # is the label that admits its listener.
    assert peer["podSelector"]["matchLabels"] == service["spec"]["selector"]
    assert one(fence_rule["ports"]) == {"port": port["port"], "protocol": "TCP"}


def test_harness_runtimes_share_capacity_but_not_agent_resources(k8s_dir: Path) -> None:
    """Claude and Codex share one namespace/image, but retain Agent-owned resources."""
    namespace = yaml.safe_load((k8s_dir / "haku/runtime-namespace/namespace.yaml").read_text())
    assert namespace["metadata"]["name"] == "haku-runtime-sandbox"

    claude_template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml").read_text())
    codex_template = yaml.safe_load(
        (k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-public-coder-codex.yaml").read_text()
    )
    assert (
        claude_template["metadata"]["namespace"]
        == codex_template["metadata"]["namespace"]
        == namespace["metadata"]["name"]
    )
    claude_pod = claude_template["spec"]["podTemplate"]
    codex_pod = codex_template["spec"]["podTemplate"]
    assert claude_pod["metadata"]["labels"] == {
        "app.kubernetes.io/name": "haku-harness-runner",
        "haku.allegedly.works/access-profile-id": "haku",
    }
    assert codex_pod["metadata"]["labels"] == {
        "app.kubernetes.io/name": "haku-harness-runner",
        "haku.allegedly.works/access-profile-id": "public-coder",
    }
    for pod in (claude_pod["spec"], codex_pod["spec"]):
        assert pod["automountServiceAccountToken"] is False
        assert "serviceAccountName" not in pod
    claude_container = one(claude_pod["spec"]["containers"])
    codex_container = one(codex_pod["spec"]["containers"])
    assert claude_container["image"] == codex_container["image"]
    assert claude_container["args"] == ["--harness", "claude"]
    assert codex_container["args"] == ["--harness", "codex-app-server"]

    claude_pool = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxwarmpool-haku-claude.yaml").read_text())
    codex_pool = yaml.safe_load(
        (k8s_dir / "haku/workspaces/app/sandboxwarmpool-haku-public-coder-codex.yaml").read_text()
    )
    assert claude_pool["metadata"]["namespace"] == codex_pool["metadata"]["namespace"] == "haku-runtime-sandbox"
    assert claude_pool["spec"]["sandboxTemplateRef"] != codex_pool["spec"]["sandboxTemplateRef"]


def sandbox_env(template: dict[str, object]) -> dict[str, dict[str, Any]]:
    container = cast(dict[str, Any], template["spec"]["podTemplate"]["spec"]["containers"][0])  # type: ignore[index]
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


def test_haku_harness_runner_has_one_neutral_publication(k8s_dir: Path) -> None:
    """The Claude template follows the one provider-neutral image repository and policy."""
    canonical_name = "haku-harness-runner"
    retired_name = "haku-claude-runner"
    flux_dir = k8s_dir / "flux-image-automation-ghcr"

    image_documents = list(yaml.safe_load_all((flux_dir / f"{canonical_name}-image.yaml").read_text()))
    repository = one(document for document in image_documents if document["kind"] == "ImageRepository")
    policy = one(document for document in image_documents if document["kind"] == "ImagePolicy")
    assert repository["metadata"]["name"] == canonical_name
    assert repository["spec"]["image"] == f"ghcr.io/agentydragon/{canonical_name}"
    assert policy["metadata"]["name"] == canonical_name
    assert policy["spec"]["imageRepositoryRef"]["name"] == canonical_name

    flux_kustomization = yaml.safe_load((flux_dir / "kustomization.yaml").read_text())
    assert f"{canonical_name}-image.yaml" in flux_kustomization["resources"]

    receiver = yaml.safe_load((k8s_dir / "flux-webhook/github-webhook-receiver.yaml").read_text())
    image_repositories = {
        resource["name"] for resource in receiver["spec"]["resources"] if resource["kind"] == "ImageRepository"
    }
    assert canonical_name in image_repositories
    assert retired_name not in image_repositories

    template_path = k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml"
    template_text = template_path.read_text()
    container = one(yaml.safe_load(template_text)["spec"]["podTemplate"]["spec"]["containers"])
    image_repository, image_tag = container["image"].rsplit(":", 1)
    assert image_repository == f"ghcr.io/agentydragon/{canonical_name}"
    assert re.fullmatch(policy["spec"]["filterTags"]["pattern"], image_tag)
    assert f'# {{"$imagepolicy": "flux-system:{canonical_name}"}}' in template_text
    assert container["args"] == ["--harness", "claude"]

    manifests = "\n".join(path.read_text() for path in k8s_dir.rglob("*.yaml"))
    assert retired_name not in manifests


def test_claude_sandbox_can_reach_the_forgejo_the_bootstrap_clones_from(k8s_dir: Path) -> None:
    """The clone target and the egress policy that permits it must not drift apart."""
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    url = one(re.findall(r"HAKU_STATE_URL:-http://([a-z0-9-]+)\.([a-z0-9-]+):(\d+)/", script))
    _, namespace, port = url

    egress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml").read_text())
    allowed = {
        (rule["toEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"], ports["port"])
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule
        for entry in rule.get("toPorts", [])
        for ports in entry["ports"]
    }
    assert (namespace, port) in allowed


def test_haku_runtimes_and_access_profile_share_one_grant(k8s_dir: Path) -> None:
    """Both sandboxes reach Kubernetes only through Console, under one shared grant."""
    binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    role = yaml.safe_load((k8s_dir / "haku/rbac/role.yaml").read_text())
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    subjects = {(s["kind"], s["name"], s.get("namespace")) for s in binding["subjects"]}
    # The ServiceAccount subject stays: ordinary pods that do carry a credential — the
    # managed-agent worker — run as it. What must hold is that the group Console SARs for a
    # proxied request resolves to that same Role, so mediating access never widens or narrows it.
    assert subjects == {("ServiceAccount", "haku", "haku-sandbox"), ("Group", "haku:access-profile:haku", None)}

    exec_target = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku.yaml").read_text())
    runner_template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml").read_text())
    assert runner_template["metadata"]["namespace"] == "haku-runtime-sandbox"
    runner_environment = sandbox_env(runner_template)
    assert runner_environment["GITHUB_TOKEN"] == {"name": "GITHUB_TOKEN", "value": "github-token-placeholder"}
    assert "HAKU_GITHUB_TOKEN" not in runner_environment

    for template in (exec_target, runner_template):
        pod = template["spec"]["podTemplate"]["spec"]
        assert pod["automountServiceAccountToken"] is False, template["metadata"]["name"]
        assert "serviceAccountName" not in pod, template["metadata"]["name"]

    # Removing the mount and supplying the proxy are one decision: a box with neither has no
    # path to the API at all, and would fail at `kubectl` rather than at deploy.
    exec_env = {
        entry["name"]: entry.get("value")
        for entry in exec_target["spec"]["podTemplate"]["spec"]["containers"][0]["env"]
    }
    assert "haku-kube-api-proxy" in exec_env["HAKU_KUBERNETES_PROXY_URL"]

    # No grant inside the runtime namespace itself: full CRUD there would let a session create
    # further pods behind a credential-mediating proxy, which is what its isolation is for.
    runtime_ns = k8s_dir / "haku/runtime-namespace"
    kinds = {
        document["kind"]
        for path in runtime_ns.glob("*.yaml")
        if path.name != "kustomization.yaml"
        for document in yaml.safe_load_all(path.read_text())
    }
    assert not kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}


def test_public_coder_and_haku_configured_diagnostics_are_secret_free(k8s_dir: Path) -> None:
    """Configured public diagnostics do not widen secret or exec access."""
    agent_readable_metadata_label = "rbac.ducktape.io/agent-readable-metadata"
    agent_readable_logs_label = "rbac.ducktape.io/agent-readable-logs"
    expected_namespace_labels = {
        k8s_dir / "agents/agent-sandbox/controller/patches.yaml": agent_readable_metadata_label,
        k8s_dir / "agents/public-coder-agent/namespace/namespace.yaml": agent_readable_metadata_label,
        k8s_dir / "nix-cache/namespace/namespace.yaml": agent_readable_metadata_label,
        k8s_dir / "vm-images-publisher/namespace.yaml": agent_readable_metadata_label,
        k8s_dir / "cli-proxy-api/namespace.yaml": agent_readable_logs_label,
        k8s_dir / "grocy/sf/app/namespace.yaml": agent_readable_logs_label,
        k8s_dir / "grocy/vallejo/app/namespace.yaml": agent_readable_logs_label,
        k8s_dir / "haku-ci/namespace.yaml": agent_readable_logs_label,
        k8s_dir / "monitoring/loki/namespace.yaml": agent_readable_logs_label,
        get_required_path("_main/props/deploy/namespace/namespace.yaml"): agent_readable_logs_label,
    }
    for path, expected_label in expected_namespace_labels.items():
        namespace = one(obj for obj in yaml.safe_load_all(path.read_text()) if obj["kind"] == "Namespace")
        labels = namespace["metadata"]["labels"]
        assert labels[expected_label] == "true", path
        assert not ({agent_readable_metadata_label, agent_readable_logs_label} - {expected_label}) & labels.keys(), path

    for relative_path in ("matrix/namespace/namespace.yaml", "x/haku/dispatch/namespace/namespace.yaml"):
        path = k8s_dir / relative_path
        namespace = one(obj for obj in yaml.safe_load_all(path.read_text()) if obj["kind"] == "Namespace")
        assert (
            not {agent_readable_metadata_label, agent_readable_logs_label} & namespace["metadata"]["labels"].keys()
        ), relative_path

    flux_system_kustomization = (k8s_dir / "flux-system/kustomization.yaml").read_text()
    assert "path: /metadata/labels/rbac.ducktape.io~1agent-readable-logs" in flux_system_kustomization
    assert 'value: "true"' in flux_system_kustomization

    metadata_role = yaml.safe_load(
        (k8s_dir / "agents/agent-rbac-base/clusterrole-agent-readable-namespace-metadata.yaml").read_text()
    )
    metadata_resources = set().union(*(set(rule["resources"]) for rule in metadata_role["rules"]))
    assert metadata_role["metadata"]["name"] == "agent-readable-namespace-metadata"
    assert all(rule["verbs"] == ["get", "list", "watch"] for rule in metadata_role["rules"])
    assert (
        not {
            "secrets",
            "externalsecrets",
            "secretstores",
            "clustersecretstores",
            "pushsecrets",
            "clusterpushsecrets",
            "pods/log",
            "pods/exec",
            "pods/attach",
            "pods/portforward",
        }
        & metadata_resources
    )
    metadata_rules = {one(rule["apiGroups"]): set(rule["resources"]) for rule in metadata_role["rules"]}
    assert metadata_rules == {
        "": {"pods", "services", "configmaps", "persistentvolumeclaims", "events"},
        "apps": {"deployments", "replicasets", "statefulsets", "daemonsets"},
        "batch": {"jobs", "cronjobs"},
        "autoscaling": {"horizontalpodautoscalers"},
        "autoscaling.k8s.io": {"verticalpodautoscalers"},
        "policy": {"poddisruptionbudgets"},
        "networking.k8s.io": {"ingresses", "networkpolicies"},
        "gateway.networking.k8s.io": {"gateways", "httproutes", "tlsroutes", "grpcroutes"},
        "image.toolkit.fluxcd.io": {"imagerepositories", "imagepolicies", "imageupdateautomations"},
    }

    logs_role = yaml.safe_load(
        (k8s_dir / "agents/agent-rbac-base/clusterrole-agent-readable-namespace-logs.yaml").read_text()
    )
    assert logs_role["metadata"]["name"] == "agent-readable-namespace-logs"
    assert logs_role["rules"] == [{"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]

    haku_subjects = {
        ("Group", "oidc-ksbx-groups:haku", None),
        ("Group", "haku:access-profile:haku", None),
        ("ServiceAccount", "haku", "haku-sandbox"),
    }
    public_coder_subject = ("Group", "haku:access-profile:public-coder", None)

    expected_roles = {
        "clickhouse/cluster/agent-diagnostics-rbac.yaml": {
            "clickhouse.altinity.com": {"clickhouseinstallations"},
            "clickhouse-keeper.altinity.com": {"clickhousekeeperinstallations"},
            "helm.toolkit.fluxcd.io": {"helmreleases"},
            "batch": {"jobs"},
            "policy": {"poddisruptionbudgets"},
            "grafana.integreatly.org": {"grafanadashboards", "grafanadatasources"},
            "monitoring.coreos.com": {"podmonitors", "servicemonitors"},
        },
        "haku/console/agent-diagnostics-rbac.yaml": {"": {"pods", "events", "configmaps"}, "apps": {"deployments"}},
        "agents/public-coder-agent/k8s-reader/extended-diagnostics-reader.yaml": {
            "volsync.backube": {"replicationsources", "replicationdestinations"}
        },
    }
    expected_kustomization_resources = {
        "clickhouse/cluster/kustomization.yaml": "agent-diagnostics-rbac.yaml",
        "haku/console/kustomization.yaml": "agent-diagnostics-rbac.yaml",
        "agents/public-coder-agent/k8s-reader/kustomization.yaml": "extended-diagnostics-reader.yaml",
    }
    for relative_path, resource in expected_kustomization_resources.items():
        kustomization = yaml.safe_load((k8s_dir / relative_path).read_text())
        assert resource in kustomization["resources"], relative_path
    public_coder_kustomization = yaml.safe_load(
        (k8s_dir / "agents/public-coder-agent/k8s-reader/kustomization.yaml").read_text()
    )
    assert "cluster-metadata-reader.yaml" in public_coder_kustomization["resources"]

    for relative_path, expected_rules in expected_roles.items():
        objects = list(yaml.safe_load_all((k8s_dir / relative_path).read_text()))
        role = one(obj for obj in objects if obj["kind"] == "Role")
        binding = one(obj for obj in objects if obj["kind"] == "RoleBinding")
        assert binding["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role["metadata"]["name"],
        }
        subjects = {(item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]}
        assert subjects == haku_subjects | {public_coder_subject}
        actual_rules = {one(rule["apiGroups"]): set(rule["resources"]) for rule in role["rules"]}
        assert actual_rules == expected_rules
        assert all(rule["verbs"] == ["get", "list", "watch"] for rule in role["rules"])
        assert not {"secrets", "pods/log", "pods/exec"} & set().union(*actual_rules.values())

    cluster_objects = list(
        yaml.safe_load_all((k8s_dir / "agents/public-coder-agent/k8s-reader/cluster-metadata-reader.yaml").read_text())
    )
    cluster_role = one(obj for obj in cluster_objects if obj["kind"] == "ClusterRole")
    cluster_binding = one(obj for obj in cluster_objects if obj["kind"] == "ClusterRoleBinding")
    assert cluster_role["rules"] == [
        {
            "apiGroups": ["apiextensions.k8s.io"],
            "resources": ["customresourcedefinitions"],
            "verbs": ["get", "list", "watch"],
        },
        {"apiGroups": ["metrics.k8s.io"], "resources": ["nodes"], "verbs": ["get", "list"]},
    ]
    cluster_subjects = {(item["kind"], item["name"], item.get("namespace")) for item in cluster_binding["subjects"]}
    assert cluster_subjects == haku_subjects | {public_coder_subject}

    # Haku also has the same two cluster-scoped reads through its existing,
    # secret-free cluster diagnostics binding; do not bind public-coder to that
    # much broader role merely to reuse it.
    haku_cluster_role = yaml.safe_load(
        (k8s_dir / "agents/agent-rbac-base/clusterrole-cluster-diagnostics-reader.yaml").read_text()
    )
    haku_cluster_rules = {
        (one(rule["apiGroups"]), resource) for rule in haku_cluster_role["rules"] for resource in rule["resources"]
    }
    assert ("apiextensions.k8s.io", "customresourcedefinitions") in haku_cluster_rules
    assert ("metrics.k8s.io", "nodes") in haku_cluster_rules
    haku_cluster_binding = yaml.safe_load(
        (k8s_dir / "agents/shared-rbac/clusterrolebinding-cluster-diagnostics-reader.yaml").read_text()
    )
    bound_haku_subjects = {
        (item["kind"], item["name"], item.get("namespace")) for item in haku_cluster_binding["subjects"]
    }
    assert haku_subjects <= bound_haku_subjects
    assert public_coder_subject not in bound_haku_subjects


def test_public_coder_kubernetes_proxy_contract(k8s_dir: Path) -> None:
    """Agent traffic, configured SAR authorization, and proxy execution authority stay separate."""
    agent_dir = k8s_dir / "agents" / "public-coder-agent"
    console_dir = k8s_dir / "haku" / "console"

    kubeconfig = yaml.safe_load((agent_dir / "app" / "agent-kubeconfig.yaml").read_text())
    cluster = one(kubeconfig["clusters"])["cluster"]
    user = one(kubeconfig["users"])["user"]
    assert cluster["server"] == "https://haku-kubeapi.allegedly.works"
    assert user["token"] == "proxy-haku-console-placeholder"

    iron = yaml.safe_load((agent_dir / "proxy" / "iron.yaml").read_text())
    secrets_transform = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")
    secrets = secrets_transform["config"]["secrets"]
    secrets_by_env = {entry["source"]["var"]: entry for entry in secrets}
    haku_secret = secrets_by_env["HAKU_CONSOLE_TOKEN"]
    assert {rule["host"] for rule in haku_secret["rules"]} == {"haku.allegedly.works", "haku-kubeapi.allegedly.works"}
    assert "KUBERNETES_READER_TOKEN" not in secrets_by_env

    ingress_policy = yaml.safe_load((agent_dir / "proxy" / "cnp-ingress.yaml").read_text())
    ingress_rule = one(ingress_policy["spec"]["ingress"])
    assert {frozenset(endpoint["matchLabels"].items()) for endpoint in ingress_rule["fromEndpoints"]} == {
        frozenset(
            {
                "k8s:io.kubernetes.pod.namespace": "public-coder-agent",
                "k8s:app.kubernetes.io/name": "public-coder-agent",
            }.items()
        ),
        frozenset(
            {
                "k8s:io.kubernetes.pod.namespace": "public-coder-agent",
                "k8s:kubevirt.io/domain": "public-coder-devbox",
            }.items()
        ),
    }
    assert one(ingress_rule["toPorts"])["ports"] == [{"port": "8080", "protocol": "TCP"}]

    app_egress = yaml.safe_load((agent_dir / "app" / "networkpolicy-egress.yaml").read_text())
    assert all(rule.get("to") for rule in app_egress["spec"]["egress"])
    assert not any("ipBlock" in peer for rule in app_egress["spec"]["egress"] for peer in rule["to"])
    assert not {port["port"] for rule in app_egress["spec"]["egress"] for port in rule.get("ports", [])} & {443, 6443}
    proxy_egress = one(
        rule
        for rule in app_egress["spec"]["egress"]
        if rule["to"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "public-coder-agent-proxy"}}}]
    )
    assert proxy_egress["ports"] == [{"port": 8080, "protocol": "TCP"}]

    proxy_deployment = yaml.safe_load((agent_dir / "proxy" / "deployment.yaml").read_text())
    proxy_container = one(proxy_deployment["spec"]["template"]["spec"]["containers"])
    proxy_env = {entry["name"]: entry for entry in proxy_container["env"]}
    assert "KUBERNETES_READER_TOKEN" not in proxy_env
    assert proxy_env["AIQUOTA_API_BEARER_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "aiquota-api-bearer-public-coder",
        "key": "bearer-token",
    }
    assert "LITELLM_API_KEY" not in proxy_env
    assert "LITELLM_API_KEY" not in secrets_by_env

    app_deployment = yaml.safe_load((agent_dir / "app" / "deployment.yaml").read_text())
    app_container = one(app_deployment["spec"]["template"]["spec"]["containers"])
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    assert "HAKU_GITHUB_TOKEN" not in app_env
    assert app_env["AIQUOTA_API_BEARER_TOKEN"] == {
        "name": "AIQUOTA_API_BEARER_TOKEN",
        "value": "proxy-aiquota-api-bearer-placeholder",
    }

    aiquota_mirror = yaml.safe_load((k8s_dir / "aiquota" / "public-coder-bearer-eso.yaml").read_text())
    assert aiquota_mirror["metadata"] == {"name": "aiquota-api-bearer-public-coder", "namespace": "cli-proxy-api"}
    assert aiquota_mirror["spec"]["secretStoreRef"] == {
        "kind": "ClusterSecretStore",
        "name": "kubernetes-cli-proxy-api-secret-store",
    }
    assert aiquota_mirror["spec"]["target"]["name"] == "aiquota-api-bearer-public-coder"
    annotations = aiquota_mirror["spec"]["target"]["template"]["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == "public-coder-agent"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == "public-coder-agent"
    assert aiquota_mirror["spec"]["data"] == [
        {"secretKey": "bearer-token", "remoteRef": {"key": "aiquota-api-bearer", "property": "bearer-token"}}
    ]

    aiquota_secret = secrets_by_env["AIQUOTA_API_BEARER_TOKEN"]
    assert aiquota_secret["replace"] == {
        "proxy_value": "proxy-aiquota-api-bearer-placeholder",
        "match_headers": ["Authorization"],
    }
    assert aiquota_secret["rules"] == [
        {"host": "aiquota.allegedly.works", "methods": ["CONNECT"]},
        {"host": "aiquota.allegedly.works", "methods": ["GET"], "paths": ["/v1/quotas", "/v1/providers/*/raw"]},
    ]
    console_config = yaml.safe_load((console_dir / "config.yaml").read_text())
    subject = console_config["kubernetes_authorization"]["subjects_by_access_profile"]["public-coder"]
    assert subject == {
        "username": "haku:access-profile:public-coder",
        "groups": ["haku:access-profile:public-coder", "system:authenticated"],
    }

    authorization_objects = list(yaml.safe_load_all((console_dir / "kubernetes-authorization-rbac.yaml").read_text()))
    execution_service_account = one(obj for obj in authorization_objects if obj["kind"] == "ServiceAccount")
    assert execution_service_account["metadata"] == {
        "name": "haku-kube-api-proxy",
        "namespace": "haku-console",
        "annotations": {"description": "Executes only Kubernetes requests authorized synchronously by Haku Console."},
    }

    proxy_objects = list(yaml.safe_load_all((console_dir / "kube-api-proxy.yaml").read_text()))
    haku_proxy = one(
        obj for obj in proxy_objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "haku-kube-api-proxy"
    )
    assert haku_proxy["spec"]["template"]["spec"]["serviceAccountName"] == "haku-kube-api-proxy"
    haku_proxy_container = one(haku_proxy["spec"]["template"]["spec"]["containers"])
    assert haku_proxy_container["image"].startswith("ghcr.io/agentydragon/haku-kube-api-proxy:devel-")
    assert haku_proxy_container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    route = one(obj for obj in proxy_objects if obj["kind"] == "HTTPRoute")
    assert route["spec"]["hostnames"] == ["haku-kubeapi.allegedly.works"]

    ceiling = yaml.safe_load((agent_dir / "k8s-reader" / "cluster-admin-ceiling.yaml").read_text())
    assert ceiling["kind"] == "ClusterRoleBinding"
    assert ceiling["metadata"]["name"] == "haku-kube-api-proxy-cluster-admin-ceiling"
    assert ceiling["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "cluster-admin",
    }
    assert ceiling["subjects"] == [
        {"kind": "ServiceAccount", "name": "haku-kube-api-proxy", "namespace": "haku-console"}
    ]

    configured_subject = {
        "kind": "Group",
        "name": "haku:access-profile:public-coder",
        "apiGroup": "rbac.authorization.k8s.io",
    }
    haku_configured_subjects = {
        ("Group", "oidc-ksbx-groups:haku", None),
        ("Group", "haku:access-profile:haku", None),
        ("ServiceAccount", "haku", "haku-sandbox"),
    }
    configured_binding_files = (
        agent_dir / "k8s-reader" / "role.yaml",
        agent_dir / "k8s-reader" / "node-reader.yaml",
        agent_dir / "k8s-reader" / "cluster-metadata-reader.yaml",
        agent_dir / "k8s-reader" / "extended-diagnostics-reader.yaml",
        k8s_dir / "clickhouse" / "cluster" / "agent-diagnostics-rbac.yaml",
        k8s_dir / "ducktape-flux" / "ducktape-flux-reader.yaml",
        console_dir / "agent-diagnostics-rbac.yaml",
    )
    configured_role_refs = {
        (obj["metadata"].get("namespace"), obj["roleRef"]["kind"], obj["roleRef"]["name"])
        for path in configured_binding_files
        for obj in yaml.safe_load_all(path.read_text())
        if obj["kind"] in {"RoleBinding", "ClusterRoleBinding"} and configured_subject in obj["subjects"]
    }
    assert configured_role_refs == {
        ("public-coder-agent", "Role", "public-coder-agent-reader"),
        ("public-coder-agent", "Role", "agent-public-coder-extended-diagnostics-reader"),
        (None, "ClusterRole", "public-coder-agent-node-reader"),
        (None, "ClusterRole", "public-coder-agent-cluster-metadata-reader"),
        ("clickhouse", "Role", "agent-clickhouse-diagnostics-reader"),
        ("ducktape-flux", "Role", "ducktape-flux-reader"),
        ("haku-console", "Role", "agent-haku-console-metadata-reader"),
    }
    assert configured_subject not in ceiling["subjects"]
    subjects_by_role_ref: dict[tuple[str | None, str, str], set[tuple[str, str, str | None]]] = {}
    for path in configured_binding_files:
        for binding in yaml.safe_load_all(path.read_text()):
            if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
                continue
            role_ref = (binding["metadata"].get("namespace"), binding["roleRef"]["kind"], binding["roleRef"]["name"])
            subjects_by_role_ref.setdefault(role_ref, set()).update(
                (item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]
            )
    for role_ref in configured_role_refs:
        assert haku_configured_subjects <= subjects_by_role_ref[role_ref], role_ref

    reader_kustomization = yaml.safe_load((agent_dir / "k8s-reader" / "kustomization.yaml").read_text())
    assert "serviceaccount.yaml" not in reader_kustomization["resources"]
    assert "cluster-admin-ceiling.yaml" in reader_kustomization["resources"]
    assert "proxy-ceiling.yaml" not in reader_kustomization["resources"]
    assert "all-pods-read-ceiling.yaml" not in reader_kustomization["resources"]

    reader_flux = yaml.safe_load((agent_dir / "k8s-reader" / "flux-kustomization.yaml").read_text())
    proxy_flux = yaml.safe_load((agent_dir / "proxy" / "flux-kustomization.yaml").read_text())
    console_flux = yaml.safe_load((console_dir / "flux-kustomization.yaml").read_text())
    cutover_label = "haku.allegedly.works/kubernetes-cutover"
    assert all(
        cutover_label not in flux["metadata"].get("labels", {}) for flux in (reader_flux, proxy_flux, console_flux)
    )
    dependency_by_name = {entry["name"]: entry for entry in proxy_flux["spec"]["dependsOn"]}
    for dependency_name in ("public-coder-agent-k8s-reader", "haku-runtime-namespace", "aiquota", "litellm-keys-tf"):
        assert "readyExpr" not in dependency_by_name[dependency_name]
    assert dependency_by_name["aiquota"]["namespace"] == "ducktape-flux"
    assert dependency_by_name["haku-runtime-namespace"]["namespace"] == "ducktape-flux"
    assert dependency_by_name["litellm-keys-tf"]["namespace"] == "ducktape-flux"
    assert "haku-console" not in dependency_by_name
    assert proxy_flux["spec"]["wait"] is True
    assert proxy_flux["spec"]["retryInterval"] == "1m"
    assert proxy_flux["spec"]["healthChecks"] == [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "public-coder-agent-proxy",
            "namespace": "public-coder-agent",
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "name": "public-coder-agent-proxy-root-ca",
            "namespace": "public-coder-agent",
        },
    ]


def test_public_coder_codex_has_empty_workspace_and_shared_trust_path(k8s_dir: Path) -> None:
    """The Web-launched Codex pair has public-coder placement, prompt and credentials."""
    namespace = "haku-runtime-sandbox"
    template_path = k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-public-coder-codex.yaml"
    template_text = template_path.read_text()
    template = yaml.safe_load(template_text)
    assert template["metadata"]["namespace"] == namespace
    pod = template["spec"]["podTemplate"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod
    container = one(pod["containers"])
    assert container["image"].startswith("ghcr.io/agentydragon/haku-harness-runner:devel-")
    assert '# {"$imagepolicy": "flux-system:haku-harness-runner"}' in template_text
    assert container["args"] == ["--harness", "codex-app-server"]
    environment = sandbox_env(template)
    assert environment["HAKU_RUNNER_WEBSOCKET_URL"]["value"] == (
        "ws://haku-console.haku-console.svc.cluster.local:9090/internal/claude/runner"
    )
    # Delete with the template's CLEANUP: the legacy spelling rides along, same value, while
    # runner images that predate the rename may still be pinned.
    assert (
        environment["HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"]["value"]
        == (environment["HAKU_RUNNER_WEBSOCKET_URL"]["value"])
    )
    assert environment["OPENAI_API_KEY"] == {
        "name": "OPENAI_API_KEY",
        "value": "proxy-litellm-public-coder-placeholder",
    }
    assert environment["HAKU_RUNNER_SETUP"]["value"] == ""
    assert not {"HAKU_GIT_USERNAME", "HAKU_GIT_PASSWORD"} & environment.keys()
    workspace = one(volume for volume in pod["volumes"] if volume["name"] == "workspace")
    assert workspace == {"name": "workspace", "emptyDir": {"sizeLimit": "10Gi"}}
    # The runner trusts the colocated Console egress fence it now routes through (#4670): the bundle
    # mounted at the system trust path is the fence CA (haku-egress-proxy-ca-cert), replacing the
    # former dedicated runner proxy's, so GnuTLS git and everything else verify the fence's leaves.
    trust_mount = one(mount for mount in container["volumeMounts"] if mount["name"] == "egress-proxy-ca")
    assert trust_mount["mountPath"] == "/etc/ssl/certs/ca-certificates.crt"
    assert trust_mount["subPath"] == "ca-certificates.crt"
    trust_volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-proxy-ca")
    assert trust_volume["configMap"]["name"] == "haku-egress-proxy-ca-cert"

    policy_objects = list(yaml.safe_load_all((k8s_dir / "haku/runtime-namespace/networkpolicy.yaml").read_text()))
    egress = one(obj for obj in policy_objects if obj["metadata"]["name"] == "public-coder-runner-egress")
    destinations = {
        (
            one(rule["toEndpoints"])["matchLabels"]["k8s:io.kubernetes.pod.namespace"],
            one(rule["toEndpoints"])["matchLabels"].get("k8s:app.kubernetes.io/name"),
            int(one(one(rule["toPorts"])["ports"])["port"]),
        )
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule and len(one(rule["toPorts"])["ports"]) == 1
    }
    assert destinations == {
        ("haku-console", "haku-console", 8080),
        # The colocated Console egress fence (#4670): the runner's HTTPS_PROXY points here.
        ("haku-console", "haku-console", 8888),
        ("haku-console", "haku-kube-api-proxy", 8443),
    }
    # LiteLLM is reached only THROUGH the fence, never a direct runner egress; the fence is on the
    # haku-console pod, so no runner rule targets the litellm or haku-egress-proxy namespaces.
    assert not any(target_namespace in {"litellm", "haku-egress-proxy"} for target_namespace, _, _ in destinations)

    trust_objects = list(
        yaml.safe_load_all((k8s_dir / "agents/public-coder-agent/proxy/trust-bundle.yaml").read_text())
    )
    trusts = {obj["metadata"]["name"]: obj for obj in trust_objects}
    assert set(trusts) == {"public-coder-agent-proxy-ca-cert"}
    assert trusts["public-coder-agent-proxy-ca-cert"]["spec"]["target"]["namespaceSelector"] == {
        "matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["public-coder-agent", namespace]}
        ]
    }
    trust_secret_sources = {
        source["secret"]["name"]
        for source in trusts["public-coder-agent-proxy-ca-cert"]["spec"]["sources"]
        if "secret" in source
    }
    assert trust_secret_sources == {"cluster-root-ca-secret", "public-coder-agent-proxy-ca"}

    console_dir = k8s_dir / "haku" / "console"
    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text())
    console_containers = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"]}
    # The colocated egress proxy sidecar (#4942) rolls with Console. It reaches Console's decision
    # oracle over the shared pod loopback — never a Service — which is the structural half of
    # #4670's oracle constraint (acceptance criterion 14): a sidecar pointed at the Service would
    # make the oracle sandbox-reachable through it.
    assert set(console_containers) == {"server", "egress-proxy"}
    egress_env = {entry["name"]: entry for entry in console_containers["egress-proxy"]["env"]}
    assert egress_env["HAKU_EGRESS_DECIDE_URL"]["value"].startswith("http://127.0.0.1:")
    assert not (console_dir / "codex-runner-service.yaml").exists()

    shared_config = yaml.safe_load((k8s_dir / "haku/console/config.yaml").read_text())
    codex = shared_config["harnesses"]["codex_app_server"]
    assert codex["namespace"] == namespace
    assert codex["claim_prefix"] == "codex"
    assert codex["harness_label"] == "codex"
    assert codex["agent_id"] in {entry["agent_id"] for entry in shared_config["launchable_agents"]}
    assert shared_config["matrix"]["default_agent_id"] == "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2"
    implementation = codex["implementation"]
    assert implementation["kind"] == "codex_app_server"
    assert implementation["provider_id"] == "haku"
    assert implementation["api_key_env_var"] == "OPENAI_API_KEY"
    assert implementation["api_base_url"] == "http://litellm.litellm.svc.cluster.local:4000/v1"
    # Codex routes through the colocated Console egress fence (#4670), not a dedicated runner proxy.
    assert codex["https_proxy"] == "http://haku-egress-proxy.haku-console.svc.cluster.local:8888"
    assert codex["mcp_url"] == "http://haku-console.haku-console.svc.cluster.local:9090/mcp"
    assert "kubernetes_proxy_url" not in codex
    assert "litellm.litellm.svc.cluster.local" not in codex["no_proxy"]
    assert "haku-console.haku-console.svc.cluster.local" in codex["no_proxy"]
    assert "haku-kube-api-proxy.haku-console.svc.cluster.local" in codex["no_proxy"]
    assert "codex_runtime" not in shared_config["settings"]
    assert shared_config["kubernetes_authorization"]["subjects_by_access_profile"]["haku"] == {
        "username": "haku:access-profile:haku",
        "groups": ["haku:access-profile:haku", "system:authenticated"],
    }

    kube_objects = list(yaml.safe_load_all((console_dir / "kube-api-proxy.yaml").read_text()))
    kube_policy = one(obj for obj in kube_objects if obj["kind"] == "CiliumNetworkPolicy")
    runtime_profiles = {
        peer["matchLabels"]["k8s:haku.allegedly.works/access-profile-id"]
        for rule in kube_policy["spec"]["ingress"]
        for peer in rule.get("fromEndpoints", [])
        if peer["matchLabels"].get("k8s:app.kubernetes.io/name") == "haku-harness-runner"
    }
    assert runtime_profiles == {"haku", "public-coder"}
    assert {obj["metadata"]["name"] for obj in kube_objects if obj["kind"] == "Deployment"} == {"haku-kube-api-proxy"}

    # Prompts belong to launchable Agents: the codex Agent's identity template plus whatever it
    # `{% include %}`s, none of it leaking anything Haku-only.
    coder_entry = one(entry for entry in shared_config["launchable_agents"] if entry["agent_id"] == codex["agent_id"])
    prompt_path = PurePosixPath(coder_entry["system_prompt_template"])
    generated = one(
        entry
        for entry in yaml.safe_load((k8s_dir / "haku/console/kustomization.yaml").read_text())["configMapGenerator"]
        if entry["name"] == "haku-console-config"
    )
    assert prompt_path.name in generated["files"]
    assert "codex-feature-gate.conf" not in generated["files"]
    prompt = (k8s_dir / "haku/console" / prompt_path.name).read_text()
    assert "public-coder-agent" in prompt
    assert "public GitHub repositories" in prompt
    assert "workspace starts empty and ephemeral" in prompt
    assert "haku-state" not in prompt.lower()
    included_names = re.findall(r'{%\s*include\s+"([^"]+)"\s*%}', prompt)
    assert included_names, "the shared attached-chat contract rides on an include"
    for included in included_names:
        assert included in generated["files"]
        assert "haku-state" not in (k8s_dir / "haku/console" / included).read_text().lower()

    workspaces_flux = yaml.safe_load((k8s_dir / "haku/workspaces/app/flux-kustomization.yaml").read_text())
    workspace_dependencies = {entry["name"] for entry in workspaces_flux["spec"]["dependsOn"]}
    # The Codex sandbox template now mounts the fence CA (haku-egress-proxy-ca-cert) instead of the
    # former dedicated runner proxy's, so its trust-bundle generator is the ordering dependency;
    # the dropped public-coder-agent-proxy dep is gone with that mount (#4670).
    assert {"haku-runtime-namespace", "haku-egress-proxy", "litellm-keys-tf"} <= workspace_dependencies
    assert "public-coder-agent-proxy" not in workspace_dependencies


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
    db_secret = embed_env["HAKU_INDEXER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]

    # Search joins `content_embeddings` on the model key the embed role writes, so reader and writer
    # must name the same model. (The endpoint address may legitimately differ; the model may not.)
    assert server_env["HAKU_CONSOLE_EMBEDDER__MODEL"]["value"] == embed_env["HAKU_INDEXER_EMBEDDER__MODEL"]["value"]

    kustomization = yaml.safe_load((console_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    generator_files = {entry["name"]: entry["files"] for entry in kustomization["configMapGenerator"]}
    index_by_id = {index["index_id"]: index for index in config["recall_indexes"]}
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
        index_id = one(slice_config["recall_indexes"])["index_id"]
        assert index_id in index_by_id, f"{path.name} slices an unregistered index {index_id!r}"
        chunk_index_ids.add(index_id)
        assert path.name == f"indexer-chunk-{index_id}-deployment.yaml", path.name
        assert chunk["metadata"]["name"] == f"haku-indexer-chunk-{index_id}"
        assert configmap_name == f"haku-indexer-chunk-{index_id}-config"
        assert slice_name == f"indexer-chunk-{index_id}-config.yaml"

        # The slice is exactly the registry projection: this index's entry verbatim plus the Git CA
        # bundle the console reads, and nothing else — so a console-only or another index's config
        # change (or parse breakage) can never reach this pod. The config-file setting names the
        # mounted slice.
        assert slice_config == {"git_ca_bundle": config["git_ca_bundle"], "recall_indexes": [index_by_id[index_id]]}
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
        assert chunk_env["HAKU_INDEXER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == db_secret
        assert _secret_refs(server).isdisjoint(_secret_refs(chunk_container) - {forgejo_git_egress_secret})
        assert _secret_refs(chunk_container) & _secret_refs(embed_container) == {db_secret}

        # Credential minimization by index: the registry names Git-read slots only for the indexes
        # that need them, and a chunk pod binds a Git slot — from a Secret — iff its own registry
        # entry names it. The pod's env is exactly its settings contract, {config_file,
        # database_url} plus its own Git slots — in particular no embedder endpoint and no index
        # selector — and its secret set is exactly its DB role plus its own Git slots.
        git_slots = {
            index_by_id[index_id][slot]
            for slot in ("username_env_var", "password_env_var")
            if index_by_id[index_id].get(slot) is not None
        }
        for var in git_slots:
            assert "secretKeyRef" in chunk_env[var]["valueFrom"], f"registry slot {var} unbound on {index_id}"
        assert set(chunk_env) == {"HAKU_INDEXER_CONFIG_FILE", "HAKU_INDEXER_DATABASE_URL"} | git_slots
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
    password_secret = adapter_env["HAKU_MATRIX_ADAPTER_MATRIX__PASSWORD"]["valueFrom"]["secretKeyRef"]
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
    assert server_env["HAKU_CONSOLE_OPERATOR_OIDC__CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == (
        subject_secret
    )

    # The adapter resolves that subject through anchor rows written at console login, so the two
    # Deployments must name one trust domain.
    assert (
        adapter_env["HAKU_MATRIX_ADAPTER_OPERATOR_IDENTITY_TRUST_DOMAIN"]["value"]
        == server_env["HAKU_CONSOLE_OPERATOR_IDENTITY__TRUST_DOMAIN"]["value"]
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
    db_secret = adapter_env["HAKU_MATRIX_ADAPTER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
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
