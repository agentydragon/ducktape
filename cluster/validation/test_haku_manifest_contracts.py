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
    assert runtime["namespace"] == template_namespace == "haku-runtime-sandbox"
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
    bridge_service_port = next(port for port in service["spec"]["ports"] if port["port"] == 9090)
    deployment = yaml.safe_load((k8s_dir / "haku/console/deployment.yaml").read_text())
    server = next(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "server"
    )
    bridge_target_port = next(
        port["containerPort"] for port in server["ports"] if port["name"] == bridge_service_port["targetPort"]
    )
    agent_egress = yaml.safe_load(agent_egress_text)
    console_rule = next(
        rule
        for rule in agent_egress["spec"]["egress"]
        if rule.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("k8s:app.kubernetes.io/name") == "haku-console"
    )
    assert console_rule["toPorts"][0]["ports"] == [{"port": str(bridge_target_port), "protocol": "TCP"}]

    kube_proxy_rule = next(
        rule
        for rule in agent_egress["spec"]["egress"]
        if rule.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("k8s:app.kubernetes.io/name")
        == "haku-kube-api-proxy"
    )
    assert kube_proxy_rule["toPorts"][0]["ports"] == [{"port": "8080", "protocol": "TCP"}]

    kube_proxy_objects = list(yaml.safe_load_all((k8s_dir / "haku/console/kube-api-proxy.yaml").read_text()))
    kube_proxy_policy = one(obj for obj in kube_proxy_objects if obj["kind"] == "CiliumNetworkPolicy")
    runner_ingress = one(rule for rule in kube_proxy_policy["spec"]["ingress"] if "fromEndpoints" in rule)
    assert runner_ingress["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": template_namespace,
                "k8s:app.kubernetes.io/name": "haku-harness-runner",
                "k8s:haku.allegedly.works/access-profile-id": "haku",
            }
        }
    ]
    assert runner_ingress["toPorts"][0]["ports"] == [{"port": "8080", "protocol": "TCP"}]

    haku_binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    assert haku_binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
        {"kind": "Group", "name": "haku:access-profile:haku", "apiGroup": "rbac.authorization.k8s.io"},
    ]

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


def test_haku_harness_runner_has_one_neutral_publication(k8s_dir: Path) -> None:
    """The Claude template follows the one provider-neutral image repository and policy."""
    canonical_name = "haku-harness-runner"
    retired_name = "haku-claude-runner"
    flux_dir = k8s_dir / "flux-image-automation-ghcr"

    repository = yaml.safe_load((flux_dir / f"{canonical_name}-image-repository.yaml").read_text())
    policy = yaml.safe_load((flux_dir / f"{canonical_name}-image-policy.yaml").read_text())
    assert repository["metadata"]["name"] == canonical_name
    assert repository["spec"]["image"] == f"ghcr.io/agentydragon/{canonical_name}"
    assert policy["metadata"]["name"] == canonical_name
    assert policy["spec"]["imageRepositoryRef"]["name"] == canonical_name

    flux_kustomization = yaml.safe_load((flux_dir / "kustomization.yaml").read_text())
    assert f"{canonical_name}-image-repository.yaml" in flux_kustomization["resources"]
    assert f"{canonical_name}-image-policy.yaml" in flux_kustomization["resources"]

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
    """Direct Haku pods and proxied runner sessions must have one Kubernetes authority."""
    binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    role = yaml.safe_load((k8s_dir / "haku/rbac/role.yaml").read_text())
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    subjects = {(s["kind"], s["name"], s.get("namespace")) for s in binding["subjects"]}
    assert subjects == {("ServiceAccount", "haku", "haku-sandbox"), ("Group", "haku:access-profile:haku", None)}

    direct_template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku.yaml").read_text())
    direct_pod = direct_template["spec"]["podTemplate"]["spec"]
    assert direct_pod["automountServiceAccountToken"] is True
    assert ("ServiceAccount", direct_pod["serviceAccountName"], "haku-sandbox") in subjects

    runner_template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml").read_text())
    runner_pod = runner_template["spec"]["podTemplate"]["spec"]
    assert runner_template["metadata"]["namespace"] == "haku-runtime-sandbox"
    assert runner_pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in runner_pod

    # No grant inside the runtime namespace itself: full CRUD there would let a session create
    # further pods behind a credential-mediating proxy, which is what its isolation is for.
    runtime_ns = k8s_dir / "haku/runtime-namespace"
    kinds = {
        yaml.safe_load(path.read_text())["kind"]
        for path in runtime_ns.glob("*.yaml")
        if path.name != "kustomization.yaml"
    }
    assert not kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}


def test_public_coder_and_haku_standing_diagnostics_are_secret_free(k8s_dir: Path) -> None:
    """Frequently used public diagnostics stay standing without widening secret or exec access."""
    agent_readable_metadata_label = "rbac.ducktape.io/agent-readable-metadata"
    agent_readable_logs_label = "rbac.ducktape.io/agent-readable-logs"
    expected_namespace_labels = {
        "agents/agent-sandbox/controller/patches.yaml": agent_readable_metadata_label,
        "agents/public-coder-agent/namespace/namespace.yaml": agent_readable_metadata_label,
        "nix-cache/namespace/namespace.yaml": agent_readable_metadata_label,
        "vm-images-publisher/namespace.yaml": agent_readable_metadata_label,
        "cli-proxy-api/namespace.yaml": agent_readable_logs_label,
        "grocy/sf/app/namespace.yaml": agent_readable_logs_label,
        "grocy/vallejo/app/namespace.yaml": agent_readable_logs_label,
        "haku-ci/namespace.yaml": agent_readable_logs_label,
        "monitoring/loki/namespace.yaml": agent_readable_logs_label,
        "props/namespace/namespace.yaml": agent_readable_logs_label,
    }
    for path, expected_label in expected_namespace_labels.items():
        namespace = one(obj for obj in yaml.safe_load_all((k8s_dir / path).read_text()) if obj["kind"] == "Namespace")
        labels = namespace["metadata"]["labels"]
        assert labels[expected_label] == "true", path
        assert not ({agent_readable_metadata_label, agent_readable_logs_label} - {expected_label}) & labels.keys(), path

    for path in ("matrix/namespace/namespace.yaml", "x/haku/dispatch/namespace/namespace.yaml"):
        namespace = one(obj for obj in yaml.safe_load_all((k8s_dir / path).read_text()) if obj["kind"] == "Namespace")
        assert (
            not {agent_readable_metadata_label, agent_readable_logs_label} & namespace["metadata"]["labels"].keys()
        ), path

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
        "analytics/cluster/agent-diagnostics-rbac.yaml": {
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
        "analytics/cluster/kustomization.yaml": "agent-diagnostics-rbac.yaml",
        "haku/console/kustomization.yaml": "agent-diagnostics-rbac.yaml",
        "agents/public-coder-agent/k8s-reader/kustomization.yaml": "extended-diagnostics-reader.yaml",
    }
    for path, resource in expected_kustomization_resources.items():
        kustomization = yaml.safe_load((k8s_dir / path).read_text())
        assert resource in kustomization["resources"], path
    public_coder_kustomization = yaml.safe_load(
        (k8s_dir / "agents/public-coder-agent/k8s-reader/kustomization.yaml").read_text()
    )
    assert "cluster-metadata-reader.yaml" in public_coder_kustomization["resources"]

    for path, expected_rules in expected_roles.items():
        objects = list(yaml.safe_load_all((k8s_dir / path).read_text()))
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
    """Agent traffic, standing SAR policy, and proxy execution authority stay separate."""
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

    app_deployment = yaml.safe_load((agent_dir / "app" / "deployment.yaml").read_text())
    app_container = one(app_deployment["spec"]["template"]["spec"]["containers"])
    app_env = {entry["name"]: entry for entry in app_container["env"]}
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
    haku_proxy = one(obj for obj in proxy_objects if obj["kind"] == "Deployment")
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

    standing_subject = {
        "kind": "Group",
        "name": "haku:access-profile:public-coder",
        "apiGroup": "rbac.authorization.k8s.io",
    }
    haku_standing_subjects = {
        ("Group", "oidc-ksbx-groups:haku", None),
        ("Group", "haku:access-profile:haku", None),
        ("ServiceAccount", "haku", "haku-sandbox"),
    }
    standing_binding_files = (
        agent_dir / "k8s-reader" / "role.yaml",
        agent_dir / "k8s-reader" / "node-reader.yaml",
        agent_dir / "k8s-reader" / "cluster-metadata-reader.yaml",
        agent_dir / "k8s-reader" / "extended-diagnostics-reader.yaml",
        k8s_dir / "analytics" / "cluster" / "agent-diagnostics-rbac.yaml",
        k8s_dir / "ducktape-flux" / "ducktape-flux-reader.yaml",
        console_dir / "agent-diagnostics-rbac.yaml",
    )
    standing_role_refs = {
        (obj["metadata"].get("namespace"), obj["roleRef"]["kind"], obj["roleRef"]["name"])
        for path in standing_binding_files
        for obj in yaml.safe_load_all(path.read_text())
        if obj["kind"] in {"RoleBinding", "ClusterRoleBinding"} and standing_subject in obj["subjects"]
    }
    assert standing_role_refs == {
        ("public-coder-agent", "Role", "public-coder-agent-reader"),
        ("public-coder-agent", "Role", "agent-public-coder-extended-diagnostics-reader"),
        (None, "ClusterRole", "public-coder-agent-node-reader"),
        (None, "ClusterRole", "public-coder-agent-cluster-metadata-reader"),
        ("analytics", "Role", "agent-analytics-diagnostics-reader"),
        ("ducktape-flux", "Role", "ducktape-flux-reader"),
        ("haku-console", "Role", "agent-haku-console-metadata-reader"),
    }
    assert standing_subject not in ceiling["subjects"]
    subjects_by_role_ref: dict[tuple[str | None, str, str], set[tuple[str, str, str | None]]] = {}
    for path in standing_binding_files:
        for binding in yaml.safe_load_all(path.read_text()):
            if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
                continue
            role_ref = (binding["metadata"].get("namespace"), binding["roleRef"]["kind"], binding["roleRef"]["name"])
            subjects_by_role_ref.setdefault(role_ref, set()).update(
                (item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]
            )
    for role_ref in standing_role_refs:
        assert haku_standing_subjects <= subjects_by_role_ref[role_ref], role_ref

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
    for dependency_name in ("public-coder-agent-k8s-reader", "haku-console", "aiquota"):
        assert "readyExpr" not in dependency_by_name[dependency_name]
    assert dependency_by_name["aiquota"]["namespace"] == "ducktape-flux"
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
