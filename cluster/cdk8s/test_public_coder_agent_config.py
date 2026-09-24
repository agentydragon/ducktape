"""public-coder's access boundaries, across the charts that grant or carry them.

The profile's RBAC is spread over the rbac-base, Haku console, ClickHouse diagnostics,
ducktape-flux and public-coder app charts; its traffic over the app and its credential proxy.
The seam with the hand-written iron config stays in
`//cluster/validation:test_haku_public_coder_contract`, and the ClickHouse reader's in
`//cluster/validation:test_public_coder_clickhouse_reader_contract`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
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
    return _synth(public_coder_proxy.chart)


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
        "haku-console chart": console_objects,
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


def test_acceptance_secret_is_named_get_for_existing_profile_not_a_pod_credential(
    app_objects: list[dict[str, Any]], proxy_objects: list[dict[str, Any]]
) -> None:
    objects = _named(app_objects, "agentplane-acceptance-operator-reader")
    role = _one(objects, "Role")
    binding = _one(objects, "RoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert one(binding["subjects"]) == _PUBLIC_CODER_SUBJECT
    rule = one(role["rules"])
    assert rule["resources"] == ["secrets"]
    assert rule["verbs"] == ["get"]
    secret_names = set(rule["resourceNames"])
    assert secret_names

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


def test_proxy_admits_the_app(app_objects: list[dict[str, Any]], proxy_objects: list[dict[str, Any]]) -> None:
    """The app's pod carries labels the proxy's ingress admits. The devbox's rule is built from
    the devbox's own labels; the app's is spelled in public_coder_proxy, which the app imports."""
    ingress_policy = _one(proxy_objects, "CiliumNetworkPolicy", "allow-public-coder-agent-proxy-ingress")
    allowed = {
        frozenset(endpoint["matchLabels"].items())
        for endpoint in one(ingress_policy["spec"]["ingress"])["fromEndpoints"]
    }
    app = _one(app_objects, "Deployment")
    # Cilium's matchLabels selects any pod whose labels are a superset of the rule, so a covering
    # rule is one the app's actual labels satisfy -- not one matching them exactly.
    actual = frozenset({"k8s:io.kubernetes.pod.namespace": app["metadata"]["namespace"]}.items()) | frozenset(
        (f"k8s:{k}", v) for k, v in app["spec"]["template"]["metadata"]["labels"].items()
    )
    assert any(rule <= actual for rule in allowed)


def test_app_egress_reaches_the_internet_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    egress = _one(app_objects, "NetworkPolicy", "public-coder-agent-egress")["spec"]["egress"]
    assert all(rule.get("to") for rule in egress)
    assert not any("ipBlock" in peer for rule in egress for peer in rule["to"])
    assert not {port["port"] for rule in egress for port in rule.get("ports", [])} & {443, 6443}


def test_app_reaches_clickhouse_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    """ClickHouse stays out of NO_PROXY: only the proxy replaces the app's password placeholder."""
    container = one(_one(app_objects, "Deployment")["spec"]["template"]["spec"]["containers"])
    no_proxy = one(entry["value"] for entry in container["env"] if entry["name"] == "NO_PROXY").split(",")
    assert not {client.HOST, client.HOST.removesuffix(".cluster.local")} & set(no_proxy)


def test_proxy_aiquota_bearer_is_mirrored_into_its_namespace(proxy_objects: list[dict[str, Any]]) -> None:
    proxy_deployment = _one(proxy_objects, "Deployment")
    proxy_env = {
        entry["name"]: entry for entry in one(proxy_deployment["spec"]["template"]["spec"]["containers"])["env"]
    }
    aiquota_ref = proxy_env["AIQUOTA_API_BEARER_TOKEN"]["valueFrom"]["secretKeyRef"]
    aiquota_mirror = _one(_synth(aiquota.chart), "ExternalSecret", aiquota_ref["name"])
    assert aiquota_mirror["spec"]["target"]["name"] == aiquota_ref["name"]
    assert aiquota_ref["key"] in {entry["secretKey"] for entry in aiquota_mirror["spec"]["data"]}
    annotations = aiquota_mirror["spec"]["target"]["template"]["metadata"]["annotations"]
    proxy_namespace = proxy_deployment["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == proxy_namespace
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == proxy_namespace


def test_public_coder_never_exceeds_haku(
    console_objects: list[dict[str, Any]],
    app_objects: list[dict[str, Any]],
    clickhouse_diagnostics_objects: list[dict[str, Any]],
) -> None:
    """Every role public-coder is bound to, Haku is bound to as well: the profile never exceeds
    the orchestrator that dispatches to it."""
    subjects_by_role_ref: dict[tuple[str | None, str, str], set[tuple[str, str, str | None]]] = {}
    binding_sources = (
        clickhouse_diagnostics_objects,
        _synth(ducktape_flux.chart),
        console_objects,
        # Less the acceptance-operator reader, which is public-coder's alone: the secrets it names
        # are the profile's own login bootstrap (see the acceptance test above).
        [obj for obj in app_objects if obj["metadata"]["name"] != "agentplane-acceptance-operator-reader"],
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
