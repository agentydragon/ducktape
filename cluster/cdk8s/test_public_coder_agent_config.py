"""public-coder's access boundaries, across the charts that grant or carry them.

The profile's RBAC is spread over the rbac-base, Haku console, ClickHouse diagnostics,
ducktape-flux and public-coder app charts; its traffic over the app and its credential proxy.
The ClickHouse reader's hand-written Secret is checked in
`//cluster/validation:test_public_coder_clickhouse_reader_contract`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s import (
    agent_rbac_base,
    agent_shared_rbac,
    aiquota,
    ducktape_flux,
    public_coder_agent_config,
    public_coder_devbox,
    public_coder_egress,
    public_coder_proxy,
)
from cluster.cdk8s.clickhouse import client, installation
from cluster.cdk8s.haku import console_config
from cluster.cdk8s.haku.charts import console_chart

_PUBLIC_CODER_SUBJECT = {
    "kind": "Group",
    "name": console_config.PUBLIC_CODER_GROUP,
    "apiGroup": "rbac.authorization.k8s.io",
}
_HAKU_SUBJECTS = {
    ("Group", "oidc-ksbx-groups:haku", None),
    ("Group", "haku:access-profile:haku", None),
    ("ServiceAccount", "haku", "haku-sandbox"),
}
_READ = ["get", "list", "watch"]


def _synth(build: Callable[[App], Chart]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(build(Cdk8sTesting.app())))


def _subjects(binding: dict[str, Any]) -> set[tuple[str, str, str | None]]:
    return {(item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]}


def _resources(role: dict[str, Any]) -> set[str]:
    return set().union(*(set(rule["resources"]) for rule in role["rules"]))


def _named(objects: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [obj for obj in objects if obj["metadata"]["name"] == name]


def _one(objects: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind and name in {None, obj["metadata"]["name"]})


@pytest.fixture(scope="module")
def app_objects() -> list[dict[str, Any]]:
    return _synth(public_coder_agent_config.app_chart)


@pytest.fixture(scope="module")
def proxy_objects() -> list[dict[str, Any]]:
    return _synth(lambda app: public_coder_proxy.chart(app, aiquota_bearer=aiquota.PUBLIC_CODER_BEARER.secret_key))


@pytest.fixture(scope="module")
def console_objects() -> list[dict[str, Any]]:
    return _synth(console_chart)


@pytest.fixture(scope="module")
def clickhouse_diagnostics_objects() -> list[dict[str, Any]]:
    return _synth(installation.agent_diagnostics_rbac_chart)


def test_public_coder_and_haku_configured_diagnostics_are_secret_free(
    console_objects: list[dict[str, Any]],
    app_objects: list[dict[str, Any]],
    clickhouse_diagnostics_objects: list[dict[str, Any]],
) -> None:
    """Configured public diagnostics do not widen secret or exec access."""
    metadata_role = _one(_synth(agent_rbac_base.chart), "ClusterRole", "agent-readable-namespace-metadata")
    assert all(rule["verbs"] == _READ for rule in metadata_role["rules"])
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

    sources = {
        "clickhouse agent-diagnostics-rbac chart": clickhouse_diagnostics_objects,
        "haku-console chart": _named(console_objects, "agent-haku-console-metadata-reader"),
        "public-coder-agent chart": _named(app_objects, "agent-public-coder-extended-diagnostics-reader"),
    }
    for source, objects in sources.items():
        role = _one(objects, "Role")
        binding = _one(objects, "RoleBinding")
        assert binding["roleRef"]["name"] == role["metadata"]["name"], source
        assert _PUBLIC_CODER_SUBJECT in binding["subjects"], source
        assert all(rule["verbs"] == _READ for rule in role["rules"]), source
        assert not {"secrets", "pods/log", "pods/exec"} & _resources(role), source

    # public-coder's cluster-scoped reads come from its own narrow ClusterRole, never from the
    # much broader cluster-diagnostics-reader Haku is bound to.
    haku_cluster_binding = one(_synth(agent_shared_rbac.chart))
    assert _subjects(haku_cluster_binding) >= _HAKU_SUBJECTS
    assert _PUBLIC_CODER_SUBJECT not in haku_cluster_binding["subjects"]


def test_agents_no_longer_restart_the_devbox(app_objects: list[dict[str, Any]]) -> None:
    reader = _one(_named(app_objects, "public-coder-agent-reader"), "Role")
    reader_vmi_rule = one(rule for rule in reader["rules"] if "virtualmachineinstances" in rule["resources"])
    assert reader_vmi_rule["verbs"] == _READ
    assert "delete" not in {verb for rule in reader["rules"] for verb in rule["verbs"]}

    retired = _named(app_objects, "public-coder-agent-devbox-vmi-restart")
    # Existing managed Sandboxes retain a resolved reference to this Role. Keep
    # the reference readable, but revoke every permission and every static binding.
    assert not _one(retired, "Role").get("rules")
    assert not any(obj["kind"] == "RoleBinding" for obj in retired)


def test_acceptance_secret_is_named_get_for_existing_profile_not_a_pod_credential(
    app_objects: list[dict[str, Any]], proxy_objects: list[dict[str, Any]]
) -> None:
    objects = _named(app_objects, "agentplane-testing-login-reader")
    role = _one(objects, "Role")
    binding = _one(objects, "RoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert _subjects(binding) == _HAKU_SUBJECTS | {
        ("Group", console_config.PUBLIC_CODER_GROUP, None),
        ("ServiceAccount", "claude-ai", "agentplane-staging"),
    }
    rule = one(role["rules"])
    assert rule["resources"] == ["secrets"]
    assert rule["verbs"] == ["get"]
    secret_names = set(rule["resourceNames"])
    assert secret_names == {"agentplane-testing-acceptance-operator"}

    for objects in (app_objects, proxy_objects):
        deployment = _one(objects, "Deployment")
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


def test_app_egress_reaches_the_internet_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    egress = _one(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert all(rule.get("to") for rule in egress)
    assert not any("ipBlock" in peer for rule in egress for peer in rule["to"])
    assert not {port["port"] for rule in egress for port in rule.get("ports", [])} & {443, 6443}


def test_app_reaches_clickhouse_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    """ClickHouse stays out of NO_PROXY: only the proxy replaces the app's password placeholder."""
    container = one(
        c for c in _one(app_objects, "Deployment")["spec"]["template"]["spec"]["containers"] if c["name"] == "openclaw"
    )
    no_proxy = one(entry["value"] for entry in container["env"] if entry["name"] == "NO_PROXY").split(",")
    assert not {client.HTTP.fqdn, client.HTTP.host} & set(no_proxy)


def test_public_coder_never_exceeds_haku(
    console_objects: list[dict[str, Any]],
    app_objects: list[dict[str, Any]],
    clickhouse_diagnostics_objects: list[dict[str, Any]],
) -> None:
    """Every role public-coder is bound to, Haku is bound to as well: the profile never exceeds
    the orchestrator that dispatches to it."""
    subjects_by_role_ref: dict[tuple[str | None, str, str], set[tuple[str, str, str | None]]] = {}
    binding_sources = (clickhouse_diagnostics_objects, _synth(ducktape_flux.chart), console_objects, app_objects)
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


def test_openclaw_uses_relay_without_receiving_its_token(app_objects: list[dict[str, Any]]) -> None:
    pod = _one(app_objects, "Deployment")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "openclaw"
    assert pod["automountServiceAccountToken"] is False
    account = _one(app_objects, "ServiceAccount", "openclaw")
    assert account["automountServiceAccountToken"] is False
    relay = one(c for c in pod["containers"] if c["name"] == "egress-sidecar")
    assert relay["volumeMounts"] == [
        {"name": "agentplane-egress-token", "mountPath": "/var/run/agentplane-egress", "readOnly": True}
    ]
    assert {e["name"] for e in relay["env"]} == {
        "AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST",
        "AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT",
        "AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE",
    }
    for container in pod["initContainers"] + [c for c in pod["containers"] if c != relay]:
        assert "agentplane-egress-token" not in {v["name"] for v in container["volumeMounts"]}
    token = one(v for v in pod["volumes"] if v["name"] == "agentplane-egress-token")
    assert token["projected"] == {
        "defaultMode": 0o440,
        "sources": [
            {"serviceAccountToken": {"audience": "agentplane-egress", "expirationSeconds": 600, "path": "token"}}
        ],
    }
    assert {
        v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v
    } == {"data": "public-coder-agent-state-v2", "diagnostics": "public-coder-agent-diagnostics"}
    trust = one(v for v in pod["volumes"] if v["name"] == "trust")
    assert trust["configMap"]["name"] == "agentplane-egress-ca"
    container = one(c for c in pod["containers"] if c["name"] == "openclaw")
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert (
        env.items()
        >= {
            "GH_PAT": "agentplane-credential-github-pat",
            "GITHUB_TOKEN": "agentplane-credential-github-pat",
            "HAKU_CONSOLE_TOKEN": "agentplane-credential-public-coder-haku-console",
            "CLICKHOUSE_PUBLIC_CODER_PASSWORD": "agentplane-credential-public-coder-clickhouse",
            "AIQUOTA_API_BEARER_TOKEN": "agentplane-credential-aiquota-read",
            "BRAVE_API_KEY": "agentplane-credential-brave-search",
            "MATRIX_PASSWORD": "agentplane-credential-public-coder-matrix",
            "HTTP_PROXY": "http://127.0.0.1:3128",
            "HTTPS_PROXY": "http://127.0.0.1:3128",
            "http_proxy": "http://127.0.0.1:3128",
            "https_proxy": "http://127.0.0.1:3128",
        }.items()
    )
    assert public_coder_agent_config.config()["channels"]["matrix"]["proxy"] == env["HTTPS_PROXY"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_openclaw_cannot_dial_iron_or_clickhouse_directly(app_objects: list[dict[str, Any]]) -> None:
    rules = _one(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert {p["port"] for r in rules for p in r["ports"]} == {53, 8888, 2222, 4000}
    gateway = one(r for r in rules if r["ports"] == [{"port": 8888, "protocol": "TCP"}])
    assert gateway["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agentplane-staging"}},
            "podSelector": {"matchLabels": public_coder_egress.GATEWAY.pods.selector},
        }
    ]


def test_iron_only_admits_the_vm(proxy_objects: list[dict[str, Any]]) -> None:
    ingress = _one(proxy_objects, "CiliumNetworkPolicy", "allow-public-coder-agent-proxy-ingress")["spec"]["ingress"]
    source = one(one(ingress)["fromEndpoints"])["matchLabels"]
    assert source == {
        "k8s:io.kubernetes.pod.namespace": "public-coder-agent",
        **{f"k8s:{key}": value for key, value in public_coder_devbox.SSH.pods.selector.items()},
    }


def test_kubeconfig_retains_haku_identity_over_the_relay() -> None:
    config_map = _one(_synth(public_coder_agent_config.kubeconfig_chart), "ConfigMap", "kubeconfig")
    config = yaml.safe_load(config_map["data"]["config"])
    cluster = one(config["clusters"])["cluster"]
    assert cluster["server"] == "https://haku-kubeapi.allegedly.works"
    assert cluster["proxy-url"] == "http://127.0.0.1:3128"
    assert cluster["certificate-authority"] == "/etc/ssl/certs/ca-certificates.crt"
    assert one(config["users"])["user"] == {"token": "agentplane-credential-public-coder-haku-console"}


if __name__ == "__main__":
    pytest_bazel.main()
