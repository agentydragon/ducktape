"""Contracts for the public-coder Haku runtime and its access boundaries."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


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
        )
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

    proxy_flux = yaml.safe_load((agent_dir / "proxy" / "flux-kustomization.yaml").read_text())
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


def sandbox_env(template: dict[str, object]) -> dict[str, dict[str, Any]]:
    container = cast(dict[str, Any], template["spec"]["podTemplate"]["spec"]["containers"][0])  # type: ignore[index]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_public_coder_codex_uses_an_ephemeral_workspace_and_fence_trust(k8s_dir: Path) -> None:
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
    assert container["image"].startswith("git.allegedly.works/ducktape-ci/haku-harness-runner:devel-")
    assert '# {"$imagepolicy": "flux-system:haku-harness-runner"}' in template_text
    assert container["args"] == ["--harness", "codex-app-server"]
    environment = sandbox_env(template)
    assert environment["HAKU_RUNNER_WEBSOCKET_URL"]["value"] == (
        "ws://haku-console.haku-console.svc.cluster.local:9090/internal/claude/runner"
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
    assert shared_config["matrix_launch"]["default_agent_id"] == "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2"
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


if __name__ == "__main__":
    pytest_bazel.main()
