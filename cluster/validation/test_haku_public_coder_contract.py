"""Contracts for the public-coder Haku runtime and its access boundaries."""

from __future__ import annotations

from pathlib import Path

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


def test_acceptance_secret_is_named_get_for_existing_profile_not_a_pod_credential(k8s_dir: Path) -> None:
    agent_dir = k8s_dir / "agents/public-coder-agent"
    manifest = agent_dir / "k8s-reader/agentplane-acceptance-operator.yaml"
    objects = list(yaml.safe_load_all(manifest.read_text()))
    role = one(obj for obj in objects if obj["kind"] == "Role")
    binding = one(obj for obj in objects if obj["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "public-coder-agent"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["agentplane-acceptance-operator", "agentplane-testing-acceptance-operator"],
            "verbs": ["get"],
        }
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    subject = one(binding["subjects"])
    assert subject == {
        "kind": "Group",
        "name": "haku:access-profile:public-coder",
        "apiGroup": "rbac.authorization.k8s.io",
    }
    staging = yaml.safe_load(
        (k8s_dir / "agentplane-staging/agent-rbac/rolebinding-agentplane-operator.yaml").read_text()
    )
    assert subject in staging["subjects"]
    kustomization = yaml.safe_load((agent_dir / "k8s-reader/kustomization.yaml").read_text())
    assert manifest.name in kustomization["resources"]

    secret_names = set(one(role["rules"])["resourceNames"])
    for layer in ("app", "proxy"):
        deployment = yaml.safe_load((agent_dir / layer / "deployment.yaml").read_text())
        pod = deployment["spec"]["template"]["spec"]
        for container in pod.get("initContainers", []) + pod["containers"]:
            for entry in container.get("env", []):
                assert not entry["name"].startswith("AGENTPLANE_ACCEPTANCE_OPERATOR_")
                assert entry.get("valueFrom", {}).get("secretKeyRef", {}).get("name") not in secret_names
            for source in container.get("envFrom", []):
                assert source.get("secretRef", {}).get("name") not in secret_names
        for volume in pod.get("volumes", []):
            assert volume.get("secret", {}).get("secretName") not in secret_names
            for source in volume.get("projected", {}).get("sources", []):
                assert source.get("secret", {}).get("name") not in secret_names


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

    # Every actual proxy client's pod must carry labels the CNP admits, derived from the
    # clients' own manifests rather than pinned here twice -- a client retired or revived
    # without updating the CNP fails this on its own instead of relying on two hand-typed
    # literals happening to be kept in sync (see e.g. the devbox retire/revive PRs).
    ingress_policy = yaml.safe_load((agent_dir / "proxy" / "cnp-ingress.yaml").read_text())
    ingress_rule = one(ingress_policy["spec"]["ingress"])
    allowed = {frozenset(endpoint["matchLabels"].items()) for endpoint in ingress_rule["fromEndpoints"]}

    app_pod_labels = yaml.safe_load((agent_dir / "app" / "deployment.yaml").read_text())["spec"]["template"][
        "metadata"
    ]["labels"]
    devbox_pod_labels = yaml.safe_load((agent_dir / "devbox" / "virtualmachine.yaml").read_text())["spec"]["template"][
        "metadata"
    ]["labels"]
    for client_labels in (app_pod_labels, devbox_pod_labels):
        # Cilium's matchLabels selects any pod whose labels are a superset of the rule, so a
        # covering rule is one the client's actual labels satisfy -- not one matching them exactly.
        actual = frozenset({"k8s:io.kubernetes.pod.namespace": "public-coder-agent"}.items()) | frozenset(
            (f"k8s:{k}", v) for k, v in client_labels.items()
        )
        assert any(rule <= actual for rule in allowed), client_labels
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
    assert app_env["GITHUB_TOKEN"] == {"name": "GITHUB_TOKEN", "value": "proxy-github-placeholder"}
    assert app_env["GH_PAT"] == {"name": "GH_PAT", "value": "proxy-github-placeholder"}
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
    assert haku_proxy_container["image"].startswith("git.allegedly.works/ducktape-ci/haku-kube-api-proxy:devel-")
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
    for dependency_name in ("public-coder-agent-k8s-reader", "aiquota", "litellm-keys-tf"):
        assert "readyExpr" not in dependency_by_name[dependency_name]
    assert dependency_by_name["aiquota"]["namespace"] == "ducktape-flux"
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


if __name__ == "__main__":
    pytest_bazel.main()
