"""What the namespace's egress grants a sandbox: the Actions API's agent-facing surface only, and
the Kubernetes access every sandbox gets rather than the ones that opted in."""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting
from more_itertools import one

from agentplane.egress import sidecar
from cluster.cdk8s.agentplane import binding_delegation, staging, testing
from cluster.cdk8s.agentplane.app_settings import (
    ACTIVITYWATCH_READ_POLICY,
    AIQUOTA_READ_POLICY,
    BASIC_POLICY,
    COINBASE_POLICY,
    FORGEJO_FINANCE_AGENT_POLICY,
    FORGEJO_HAKU_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
    PLAID_PGWEB_POLICY,
)
from cluster.cdk8s.agentplane.conftest import NAMESPACES
from cluster.cdk8s.agentplane.egress import KUBERNETES_AUDIENCE, KUBERNETES_CREDENTIAL, KUBERNETES_HOST
from util.settings_contract import env_name

# What a workload token may reach on the Actions service: the MCP endpoint, its schema and
# the action-group/request API. The operator API (/v1/operator/*) and the OAuth endpoints
# (/register, /token, ...) stay off this policy.
_AGENT_FACING_PREFIXES = ("/mcp", "/openapi.json", "/v1/action-")


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one object of `kind` named `name`, which every test here asserts exists."""
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_workload_policy_grants_only_the_agent_facing_actions_api(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    policy = one(
        doc
        for doc in agentplane_manifests[namespace]
        if doc["kind"] == "EgressPolicy" and doc["metadata"]["name"] == BASIC_POLICY
    )
    rule = one(rule for rule in policy["spec"]["rules"] if one(rule["hosts"]).startswith("agentplane-actions."))
    assert rule["paths"], "a rule without paths admits every path on the host"
    assert all(path.startswith(_AGENT_FACING_PREFIXES) for path in rule["paths"])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_kubernetes_access_is_part_of_what_every_sandbox_is_granted(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    """Every agent talks to the API server, so the rule admitting it is in `basic`.

    `basic` is the policy every launch is granted before the caller picks anything
    (`default_policies` and `launch_policies`, agentplane/app/egress.py), which is what makes
    Kubernetes access a property of a sandbox rather than a choice one. It was its own policy
    while every SandboxTemplate already mounted the kubeconfig naming this credential
    (sandbox_pod.py): a box whose preset or caller did not name it held a working-looking config
    whose requests the proxy refused for want of a rule, a transport failure dressed up as an
    authorization answer.

    What makes it legitimate as an unconditional grant is what gets substituted. A default is the
    one grant a caller cannot decline, so it may only ever substitute the sandbox's own identity;
    this credential is that Pod's ServiceAccount projected for the API server's audience, and a
    `secretRef` source here would hand an operator's credential to every sandbox in the namespace.
    What a box may then do is the RBAC bound to its account, the only axis this narrows -- which
    is why the rule names neither methods nor paths.
    """
    docs = agentplane_manifests[namespace]
    basic = _by_name(docs, "EgressPolicy", BASIC_POLICY)
    api_server = one(rule for rule in basic["spec"]["rules"] if KUBERNETES_HOST in rule["hosts"])
    assert api_server.get("credentialRef") == {"name": KUBERNETES_CREDENTIAL}, api_server
    # Narrowing by verb or path would be a second, weaker copy of RBAC; RBAC decides.
    assert "methods" not in api_server, api_server
    assert "paths" not in api_server, api_server

    credential = _by_name(docs, "EgressCredential", KUBERNETES_CREDENTIAL)
    source = credential["spec"]["source"]
    assert "secretRef" not in source, "a default may substitute a sandbox's own credential, never an operator's"
    assert source["projectedWorkloadToken"]["audience"] == KUBERNETES_AUDIENCE, source

    # And `basic` reaches a sandbox that picks nothing, which is the whole claim.
    app_config = _by_name(docs, "ConfigMap", "agentplane-app-config")
    defaults: list[str] = yaml.safe_load(app_config["data"]["config.yaml"])["default_policies"]
    assert BASIC_POLICY in defaults, defaults


def test_testing_github_policy_has_its_credential_and_no_real_account_credentials(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    manifests = agentplane_manifests[testing.ENV.namespace]
    github = one(
        doc
        for doc in manifests
        if doc["kind"] == "EgressPolicy" and doc["metadata"]["name"] == GITHUB_AGENTYDRAGON_AGENT_POLICY
    )
    github_rule = one(github["spec"]["rules"])
    assert "codeload.github.com" in github_rule["hosts"]
    credential_name = github_rule["credentialRef"]["name"]
    credential = one(
        doc for doc in manifests if doc["kind"] == "EgressCredential" and doc["metadata"]["name"] == credential_name
    )
    assert credential["spec"]["source"]["secretRef"]
    assert not any(
        doc["kind"] in {"EgressCredential", "EgressPolicy"}
        and doc["metadata"]["name"]
        in {
            FORGEJO_HAKU_POLICY,
            FORGEJO_FINANCE_AGENT_POLICY,
            GOOGLE_READONLY_POLICY,
            GROCY_SF_READONLY_POLICY,
            HOME_ASSISTANT_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AIQUOTA_READ_POLICY,
            HAKU_MAILBOX_POLICY,
            PLAID_PGWEB_POLICY,
        }
        for doc in manifests
    )


def test_coinbase_static_grant_is_preserved_and_managed_haku_picks_scoped_grant(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    """The static OAuth account keeps its grant; managed Haku picks a separate SA binding."""
    docs = agentplane_manifests[staging.ENV.namespace]

    role_binding = _by_name(docs, "RoleBinding", "claude-ai-coinbase-reader")
    assert role_binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "claude-ai", "namespace": staging.ENV.namespace}
    ]

    egress_bindings = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "EgressBinding"}
    assert COINBASE_POLICY in egress_bindings["claude-ai"]["spec"]["policies"]
    assert COINBASE_POLICY not in egress_bindings["haku-agent"]["spec"]["policies"]

    role = _by_name(docs, "Role", "claude-ai-coinbase-reader")
    assert role["rules"] == [
        {"apiGroups": [""], "resourceNames": ["coinbase-api-credentials"], "resources": ["secrets"], "verbs": ["get"]}
    ]

    app_config = _by_name(docs, "ConfigMap", "agentplane-app-config")
    config = yaml.safe_load(app_config["data"]["config.yaml"])
    haku_preset = config["sandbox_presets"]["haku"]
    assert COINBASE_POLICY in haku_preset["policies"], haku_preset
    assert "coinbase-credentials" in haku_preset["kubernetes_grants"]
    assert config["kubernetes_grants"]["coinbase-credentials"] == {
        "kind": "RoleBinding",
        "namespace": staging.ENV.namespace,
        "role_ref": {"kind": "Role", "name": "claude-ai-coinbase-reader"},
    }


def test_haku_grant_catalog_generates_scoped_app_delegation(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    haku = config["sandbox_presets"]["haku"]
    assert haku["kubernetes_grants"][:5] == [
        "cluster-diagnostics",
        "haku-sandbox-write",
        "agentplane-testing-operator",
        "agentplane-staging-metadata",
        "agentplane-staging-logs",
    ]
    assert "coinbase-credentials" in haku["kubernetes_grants"]
    assert haku["kubernetes_grants"][-6:] == [
        "haku-console-metadata",
        "clickhouse-diagnostics",
        "ducktape-flux-read",
        "public-coder-volsync-status",
        "public-coder-agent-reader",
        "public-coder-agent-devbox-vmi-restart",
    ]
    assert config["kubernetes_grants"]["cluster-diagnostics"] == {
        "kind": "ClusterRoleBinding",
        "role_ref": {"kind": "ClusterRole", "name": "cluster-diagnostics-reader"},
    }
    assert config["kubernetes_grants"]["haku-sandbox-write"] == {
        "kind": "RoleBinding",
        "namespace": "haku-sandbox",
        "role_ref": {"kind": "Role", "name": "haku-sandbox-admin"},
    }
    assert config["kubernetes_grants"]["haku-console-metadata"] == {
        "kind": "RoleBinding",
        "namespace": "haku-console",
        "role_ref": {"kind": "Role", "name": "agent-haku-console-metadata-reader"},
    }
    assert config["kubernetes_grants"]["clickhouse-diagnostics"] == {
        "kind": "RoleBinding",
        "namespace": "clickhouse",
        "role_ref": {"kind": "Role", "name": "agent-clickhouse-diagnostics-reader"},
    }
    assert config["kubernetes_grants"]["ducktape-flux-read"] == {
        "kind": "RoleBinding",
        "namespace": "ducktape-flux",
        "role_ref": {"kind": "Role", "name": "ducktape-flux-reader"},
    }
    assert config["kubernetes_grants"]["public-coder-volsync-status"] == {
        "kind": "RoleBinding",
        "namespace": "public-coder-agent",
        "role_ref": {"kind": "Role", "name": "agent-public-coder-extended-diagnostics-reader"},
    }
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == "haku-sandbox"
        and doc["metadata"]["name"] == "agentplane-staging-managed-bindings"
        for doc in docs
    ), "the app Kustomization must not own managed-binding delegation in external namespaces"
    app_cluster_role = _by_name(docs, "ClusterRole", "agentplane-staging-managed-cluster-bindings")
    assert {
        tuple(rule.get("resourceNames", [])) for rule in app_cluster_role["rules"] if rule["verbs"] == ["bind"]
    } == {("agent-readable-namespace-logs",), ("agent-readable-namespace-metadata",), ("cluster-diagnostics-reader",)}
    assert any(
        rule["resources"] == ["clusterroles"] and rule["verbs"] == ["get"] and not rule.get("resourceNames")
        for rule in app_cluster_role["rules"]
    )
    cleanup_namespaces = set(config["kubernetes_binding_cleanup_namespaces"])
    assert {
        "agentplane-testing",
        "clickhouse",
        "ducktape-flux",
        "haku-console",
        "haku-sandbox",
        "public-coder-agent",
    } <= cleanup_namespaces
    assert staging.ENV.namespace not in cleanup_namespaces
    assert config["kubernetes_cluster_binding_cleanup"] is True


def test_managed_haku_public_coder_reader_and_restart_have_named_bind_delegation(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    for name in ("public-coder-agent-reader", "public-coder-agent-devbox-vmi-restart"):
        assert config["kubernetes_grants"][name] == {
            "kind": "RoleBinding",
            "namespace": "public-coder-agent",
            "role_ref": {"kind": "Role", "name": name},
        }
        assert name in config["sandbox_presets"]["haku"]["kubernetes_grants"]
    assert "public-coder-agent" in config["kubernetes_binding_cleanup_namespaces"]

    delegated = Cdk8sTesting.synth(binding_delegation.chart(Cdk8sTesting.app(), staging.ENV, "public-coder-agent"))
    role = _by_name(delegated, "Role", "agentplane-staging-external-bindings")
    assert role["metadata"]["namespace"] == "public-coder-agent"
    assert role["rules"][0] == {
        "apiGroups": ["rbac.authorization.k8s.io"],
        "resources": ["rolebindings"],
        "verbs": ["create", "get", "list", "delete"],
    }
    assert {tuple(rule.get("resourceNames", [])) for rule in role["rules"] if rule["verbs"] == ["bind"]} == {
        ("agent-public-coder-extended-diagnostics-reader",),
        ("public-coder-agent-reader",),
        ("public-coder-agent-devbox-vmi-restart",),
    }
    binding = _by_name(delegated, "RoleBinding", "agentplane-staging-external-bindings")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "agentplane-app", "namespace": "agentplane-staging"}
    ]
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == "public-coder-agent"
        and doc["metadata"]["name"] == "agentplane-staging-external-bindings"
        for doc in docs
    )


def test_managed_haku_testing_operator_reuses_static_role_with_external_delegation(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    staging_docs = agentplane_manifests[staging.ENV.namespace]
    config = yaml.safe_load(_by_name(staging_docs, "ConfigMap", "agentplane-app-config")["data"]["config.yaml"])
    assert config["kubernetes_grants"]["agentplane-testing-operator"] == {
        "kind": "RoleBinding",
        "namespace": "agentplane-testing",
        "role_ref": {"kind": "Role", "name": "agentplane-testing-operator"},
    }
    assert "agentplane-testing-operator" in config["sandbox_presets"]["haku"]["kubernetes_grants"]
    assert "agentplane-testing" in config["kubernetes_binding_cleanup_namespaces"]
    assert "agentplane-testing" in binding_delegation.external_scopes(staging.ENV)

    testing_docs = agentplane_manifests[testing.ENV.namespace]
    assert (
        _by_name(testing_docs, "Role", "agentplane-testing-operator")["metadata"]["namespace"] == "agentplane-testing"
    )
    static_binding = _by_name(testing_docs, "RoleBinding", "agent-agentplane-testing-operator")
    assert static_binding["roleRef"]["name"] == "agentplane-testing-operator"
    assert {subject["name"] for subject in static_binding["subjects"]} >= {
        "oidc-ksbx-groups:haku",
        "haku:access-profile:haku",
        "haku",
    }

    external_docs = Cdk8sTesting.synth(binding_delegation.chart(Cdk8sTesting.app(), staging.ENV, "agentplane-testing"))
    role = _by_name(external_docs, "Role", "agentplane-staging-external-bindings")
    assert role["metadata"]["namespace"] == "agentplane-testing"
    assert role["rules"] == [
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["rolebindings"],
            "verbs": ["create", "get", "list", "delete"],
        },
        {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles"], "verbs": ["get"]},
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resourceNames": ["agentplane-testing-operator"],
            "resources": ["roles"],
            "verbs": ["bind"],
        },
    ]
    delegated_binding = _by_name(external_docs, "RoleBinding", "agentplane-staging-external-bindings")
    assert delegated_binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "agentplane-app", "namespace": "agentplane-staging"}
    ]
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"].get("namespace") == "agentplane-testing"
        and doc["metadata"]["name"] == "agentplane-staging-external-bindings"
        for doc in staging_docs
    )


def test_upstream_bundles_have_independent_environment_ownership(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    names = set()
    for namespace, manifests in agentplane_manifests.items():
        bundle = one(
            doc
            for doc in manifests
            if doc["kind"] == "Bundle" and doc["metadata"]["name"] == f"{namespace}-egress-upstream-ca"
        )
        name = bundle["metadata"]["name"]
        assert name not in names, "cluster-scoped upstream Bundles must have separate owners"
        names.add(name)
        assert bundle["spec"]["target"]["namespaceSelector"] == {
            "matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [namespace]}]
        }
        assert bundle["spec"]["sources"] == [
            {"useDefaultCAs": True},
            {"configMap": {"name": "kube-root-ca.crt", "key": "ca.crt"}},
        ]


def test_environments_do_not_share_cluster_scoped_bundles(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    owners: dict[str, str] = {}
    for namespace, manifests in agentplane_manifests.items():
        for doc in manifests:
            if doc["kind"] != "Bundle":
                continue
            name = doc["metadata"]["name"]
            assert name not in owners, f"Bundle {name} is owned by both {owners.get(name)} and {namespace}"
            owners[name] = namespace


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_sandbox_sidecars_gate_readiness_on_the_loopback_listener(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    templates = [doc for doc in agentplane_manifests[namespace] if doc["kind"] == "SandboxTemplate"]
    assert templates
    for template in templates:
        containers = template["spec"]["podTemplate"]["spec"]["containers"]
        egress_sidecar = one(container for container in containers if container["name"] == "egress-sidecar")
        env = {variable["name"]: variable["value"] for variable in egress_sidecar["env"]}
        assert env[env_name(sidecar.Settings, "readiness_host")] == sidecar.READINESS_HOST
        readiness_port = int(env[env_name(sidecar.Settings, "readiness_port")])
        probe = egress_sidecar["readinessProbe"]["httpGet"]
        assert probe["port"] == readiness_port == sidecar.READINESS_PORT
        assert probe["path"] == sidecar.READINESS_PATH
        assert "livenessProbe" not in egress_sidecar


def test_runner_context_configuration_matches_the_verified_qwen_roster(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    expected = {
        "ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k": 128 * 1024,
        "ollama/olm-chat/qwen3.8-flash-next-iq4xs-128k": 128 * 1024,
        "ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k": 256 * 1024,
        "ollama/olm-chat/qwen3.8-flash-next-iq4xs-256k": 256 * 1024,
    }
    for namespace, manifests in agentplane_manifests.items():
        templates = [doc for doc in manifests if doc["kind"] == "SandboxTemplate"]
        runner_containers = [
            container
            for template in templates
            for container in template["spec"]["podTemplate"]["spec"]["containers"]
            if container["name"] == "runner"
        ]
        assert runner_containers, namespace
        for container in runner_containers:
            environment = {variable["name"]: variable.get("value") for variable in container.get("env", [])}
            assert json.loads(environment["AGENTPLANE_MODEL_CONTEXT_WINDOWS"]) == expected


if __name__ == "__main__":
    pytest_bazel.main()
