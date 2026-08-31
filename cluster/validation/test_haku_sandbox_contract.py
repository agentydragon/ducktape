"""Contracts for Haku sandbox and egress deployment wiring."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest_bazel
import yaml
from more_itertools import one


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
    credential = one(
        credential
        for credential in egress["credentials"].values()
        if credential["handle"] == litellm_grant["credential_handle"]
    )
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
    activity_credential = one(
        credential
        for credential in egress["credentials"].values()
        if credential["handle"] == "activitywatch-read-token"
    )
    assert activity_credential["placeholder"] == runner_environment["AW_READ_TOKEN"]["value"]
    assert "value" not in activity_credential
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


def test_public_coder_pod_uses_the_colocated_egress_fence(k8s_dir: Path) -> None:
    """The public-coder OpenClaw pod is wired into the colocated fence.

    The pod carries the fleet `inject-haku-egress-proxy` policy's own CA wiring — same volume
    name, same configMap, same mountPath — which is what every rule's precondition checks for
    (the skip itself is pinned in kyverno/test_proxy_injection.py), so a widening of the fleet
    injection to this namespace skips the pod instead of appending port-8080 env over its iron
    values. This pins the relations that make the boundary sound: the carried wiring really is the
    policy's own, the trust Bundle actually delivers that ConfigMap to the pod's namespace (else
    the pod wedges in ContainerCreating), the pod's egress NetworkPolicy admits the listener the
    Service publishes, and the Deployment does NOT cut its default proxy env over to that listener.
    This pod has no live Console bridge bearer, so it remains on its iron proxy; only a
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


def test_haku_harness_runner_image_policy_matches_the_claude_template(k8s_dir: Path) -> None:
    """The Claude template follows the shared harness-runner image repository and policy."""
    canonical_name = "haku-harness-runner"
    flux_dir = k8s_dir / "flux-image-automation-forgejo"

    image_documents = list(yaml.safe_load_all((flux_dir / f"{canonical_name}-image.yaml").read_text()))
    repository = one(document for document in image_documents if document["kind"] == "ImageRepository")
    policy = one(document for document in image_documents if document["kind"] == "ImagePolicy")
    assert repository["metadata"]["name"] == canonical_name
    assert repository["spec"]["image"] == f"git.allegedly.works/ducktape-ci/{canonical_name}"
    assert policy["metadata"]["name"] == canonical_name
    assert policy["spec"]["imageRepositoryRef"]["name"] == canonical_name

    flux_kustomization = yaml.safe_load((flux_dir / "kustomization.yaml").read_text())
    assert f"{canonical_name}-image.yaml" in flux_kustomization["resources"]

    receiver = yaml.safe_load((k8s_dir / "flux-webhook/github-webhook-receiver.yaml").read_text())
    image_repositories = {
        resource["name"] for resource in receiver["spec"]["resources"] if resource["kind"] == "ImageRepository"
    }
    assert canonical_name not in image_repositories

    template_path = k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml"
    template_text = template_path.read_text()
    container = one(yaml.safe_load(template_text)["spec"]["podTemplate"]["spec"]["containers"])
    image_repository, image_tag = container["image"].rsplit(":", 1)
    assert image_repository == f"git.allegedly.works/ducktape-ci/{canonical_name}"
    assert re.fullmatch(policy["spec"]["filterTags"]["pattern"], image_tag)
    assert f'# {{"$imagepolicy": "flux-system:{canonical_name}"}}' in template_text
    assert container["args"] == ["--harness", "claude"]


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


if __name__ == "__main__":
    pytest_bazel.main()
