"""Contracts for the public-coder Haku runtime and its access boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import aiquota

# pytest_plugins loads cluster.validation.haku_console_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:haku_console_fixtures
pytest_plugins = ("cluster.validation.haku_console_fixtures",)

_PUBLIC_CODER_SUBJECT = {
    "kind": "Group",
    "name": "haku:access-profile:public-coder",
    "apiGroup": "rbac.authorization.k8s.io",
}
_HAKU_SUBJECTS = {
    ("Group", "oidc-ksbx-groups:haku", None),
    ("Group", "haku:access-profile:haku", None),
    ("ServiceAccount", "haku", "haku-sandbox"),
}


def _subjects(binding: dict[str, Any]) -> set[tuple[str, str, str | None]]:
    return {(item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]}


def _resources(role: dict[str, Any]) -> set[str]:
    return set().union(*(set(rule["resources"]) for rule in role["rules"]))


def test_public_coder_and_haku_configured_diagnostics_are_secret_free(
    k8s_dir: Path, haku_console_objects: list[dict[str, Any]]
) -> None:
    """Configured public diagnostics do not widen secret or exec access."""
    rbac_base = list(yaml.safe_load_all((k8s_dir / "agents/agent-rbac-base/agent-rbac-base.k8s.yaml").read_text()))
    metadata_role = one(
        obj
        for obj in rbac_base
        if obj["kind"] == "ClusterRole" and obj["metadata"]["name"] == "agent-readable-namespace-metadata"
    )
    assert all(rule["verbs"] == ["get", "list", "watch"] for rule in metadata_role["rules"])
    assert not {
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
    } & _resources(metadata_role)

    logs_role = one(
        obj
        for obj in rbac_base
        if obj["kind"] == "ClusterRole" and obj["metadata"]["name"] == "agent-readable-namespace-logs"
    )
    assert logs_role["rules"] == [{"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]

    sources: dict[str, list[dict[str, Any]] | None] = {
        "clickhouse/cluster/agent-diagnostics-rbac.k8s.yaml": None,
        "haku-console chart": haku_console_objects,
        "agents/public-coder-agent/app/extended-diagnostics-reader.yaml": None,
    }
    for relative_path, chart_objects in sources.items():
        objects = (
            chart_objects
            if chart_objects is not None
            else list(yaml.safe_load_all((k8s_dir / relative_path).read_text()))
        )
        role = one(obj for obj in objects if obj["kind"] == "Role")
        binding = one(obj for obj in objects if obj["kind"] == "RoleBinding")
        assert binding["roleRef"]["name"] == role["metadata"]["name"], relative_path
        assert _PUBLIC_CODER_SUBJECT in binding["subjects"], relative_path
        assert all(rule["verbs"] == ["get", "list", "watch"] for rule in role["rules"]), relative_path
        assert not {"secrets", "pods/log", "pods/exec"} & _resources(role), relative_path

    # public-coder's cluster-scoped reads come from its own narrow ClusterRole, never from the
    # much broader cluster-diagnostics-reader Haku is bound to.
    haku_cluster_binding = yaml.safe_load((k8s_dir / "agents/shared-rbac/agent-shared-rbac.k8s.yaml").read_text())
    assert _subjects(haku_cluster_binding) >= _HAKU_SUBJECTS
    assert _PUBLIC_CODER_SUBJECT not in haku_cluster_binding["subjects"]


def test_acceptance_secret_is_named_get_for_existing_profile_not_a_pod_credential(k8s_dir: Path) -> None:
    agent_dir = k8s_dir / "agents/public-coder-agent"
    objects = list(yaml.safe_load_all((agent_dir / "app/agentplane-acceptance-operator.yaml").read_text()))
    role = one(obj for obj in objects if obj["kind"] == "Role")
    binding = one(obj for obj in objects if obj["kind"] == "RoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert one(binding["subjects"]) == _PUBLIC_CODER_SUBJECT
    rule = one(role["rules"])
    assert rule["resources"] == ["secrets"]
    assert rule["verbs"] == ["get"]
    secret_names = set(rule["resourceNames"])
    assert secret_names

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


def test_public_coder_kubernetes_proxy_contract(k8s_dir: Path, haku_console_objects: list[dict[str, Any]]) -> None:
    """Agent traffic, configured SAR authorization, and proxy execution authority stay separate."""
    agent_dir = k8s_dir / "agents" / "public-coder-agent"

    iron = yaml.safe_load((agent_dir / "proxy" / "iron.yaml").read_text())
    secrets_transform = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")
    secrets_by_env = {entry["source"]["var"]: entry for entry in secrets_transform["config"]["secrets"]}

    # The kubeconfig carries the placeholder iron swaps for the Console bearer, on the host it
    # targets -- the HTTPRoute in front of haku-kube-api-proxy.
    kubeconfig = yaml.safe_load((agent_dir / "app" / "agent-kubeconfig.yaml").read_text())
    server_host = urlparse(one(kubeconfig["clusters"])["cluster"]["server"]).hostname
    haku_secret = secrets_by_env["HAKU_CONSOLE_TOKEN"]
    assert one(kubeconfig["users"])["user"]["token"] == haku_secret["replace"]["proxy_value"]
    assert server_host in {rule["host"] for rule in haku_secret["rules"]}
    haku_proxy = one(
        obj
        for obj in haku_console_objects
        if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "haku-kube-api-proxy"
    )
    route = one(
        obj
        for obj in haku_console_objects
        if obj["kind"] == "HTTPRoute"
        and one(one(obj["spec"]["rules"])["backendRefs"])["name"] == haku_proxy["metadata"]["name"]
    )
    assert server_host in route["spec"]["hostnames"]

    # Every actual proxy client's pod must carry labels the CNP admits, derived from the
    # clients' own manifests rather than pinned here twice -- a client retired or revived
    # without updating the CNP fails this on its own instead of relying on two hand-typed
    # literals happening to be kept in sync (see e.g. the devbox retire/revive PRs).
    ingress_policy = yaml.safe_load((agent_dir / "proxy" / "cnp-ingress.yaml").read_text())
    ingress_rule = one(ingress_policy["spec"]["ingress"])
    allowed = {frozenset(endpoint["matchLabels"].items()) for endpoint in ingress_rule["fromEndpoints"]}

    app_deployment = yaml.safe_load((agent_dir / "app" / "deployment.yaml").read_text())
    app_pod_labels = app_deployment["spec"]["template"]["metadata"]["labels"]
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

    # The app holds only the placeholders iron replaces; the proxy holds the credentials.
    app_container = one(app_deployment["spec"]["template"]["spec"]["containers"])
    app_env = {entry["name"]: entry for entry in app_container["env"]}
    github_placeholder = secrets_by_env["GITHUB_TOKEN"]["replace"]["proxy_value"]
    assert app_env["GITHUB_TOKEN"]["value"] == github_placeholder
    assert app_env["GH_PAT"]["value"] == github_placeholder
    assert (
        app_env["AIQUOTA_API_BEARER_TOKEN"]["value"]
        == secrets_by_env["AIQUOTA_API_BEARER_TOKEN"]["replace"]["proxy_value"]
    )

    proxy_deployment = yaml.safe_load((agent_dir / "proxy" / "deployment.yaml").read_text())
    proxy_container = one(proxy_deployment["spec"]["template"]["spec"]["containers"])
    proxy_env = {entry["name"]: entry for entry in proxy_container["env"]}
    aiquota_ref = proxy_env["AIQUOTA_API_BEARER_TOKEN"]["valueFrom"]["secretKeyRef"]
    aiquota_objects = cast(list[dict[str, Any]], Cdk8sTesting.synth(aiquota.chart(Cdk8sTesting.app())))
    aiquota_mirror = one(
        obj
        for obj in aiquota_objects
        if obj["kind"] == "ExternalSecret" and obj["metadata"]["name"] == aiquota_ref["name"]
    )
    assert aiquota_mirror["spec"]["target"]["name"] == aiquota_ref["name"]
    assert aiquota_ref["key"] in {entry["secretKey"] for entry in aiquota_mirror["spec"]["data"]}
    annotations = aiquota_mirror["spec"]["target"]["template"]["metadata"]["annotations"]
    proxy_namespace = proxy_deployment["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == proxy_namespace
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == proxy_namespace

    # Console SARs the group the RBAC binds; the proxy executes as its own ServiceAccount, which
    # is the only subject of the cluster-admin ceiling.
    console_config = yaml.safe_load(
        one(
            obj
            for obj in haku_console_objects
            if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "haku-console-config"
        )["data"]["config.yaml"]
    )
    profile = console_config["kubernetes_authorization"]["subjects_by_access_profile"]["public-coder"]
    assert _PUBLIC_CODER_SUBJECT["name"] in profile["groups"]
    execution_name = haku_proxy["spec"]["template"]["spec"]["serviceAccountName"]
    execution_service_account = one(
        obj
        for obj in haku_console_objects
        if obj["kind"] == "ServiceAccount" and obj["metadata"]["name"] == execution_name
    )
    ceiling = yaml.safe_load((agent_dir / "app" / "cluster-admin-ceiling.yaml").read_text())
    assert ceiling["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": execution_name,
            "namespace": execution_service_account["metadata"]["namespace"],
        }
    ]

    # Every role public-coder is bound to, Haku is bound to as well: the profile never exceeds
    # the orchestrator that dispatches to it.
    subjects_by_role_ref: dict[tuple[str | None, str, str], set[tuple[str, str, str | None]]] = {}
    binding_sources = (
        *(
            yaml.safe_load_all(path.read_text())
            for path in (
                agent_dir / "app" / "role.yaml",
                agent_dir / "app" / "node-reader.yaml",
                agent_dir / "app" / "cluster-metadata-reader.yaml",
                agent_dir / "app" / "extended-diagnostics-reader.yaml",
                k8s_dir / "clickhouse" / "cluster" / "agent-diagnostics-rbac.yaml",
                k8s_dir / "flux" / "ducktape-flux" / "ducktape-flux.k8s.yaml",
            )
        ),
        haku_console_objects,
    )
    for objects in binding_sources:
        for binding in objects:
            if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
                continue
            role_ref = (binding["metadata"].get("namespace"), binding["roleRef"]["kind"], binding["roleRef"]["name"])
            subjects_by_role_ref.setdefault(role_ref, set()).update(_subjects(binding))
    public_coder_subject = (_PUBLIC_CODER_SUBJECT["kind"], _PUBLIC_CODER_SUBJECT["name"], None)
    public_coder_role_refs = {ref for ref, subjects in subjects_by_role_ref.items() if public_coder_subject in subjects}
    assert public_coder_role_refs
    for role_ref in public_coder_role_refs:
        assert subjects_by_role_ref[role_ref] >= _HAKU_SUBJECTS, role_ref


if __name__ == "__main__":
    pytest_bazel.main()
