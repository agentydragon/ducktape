"""What the namespace grants a sandbox: SVID-protected service routes and Kubernetes access."""

from __future__ import annotations

import json
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.crd_group import GROUP
from agentplane.egress.settings import Settings as EgressSettings
from agentplane.net.peertls.config import (
    APP_URI,
    CENTRAL_SAMPLER_URI,
    HISTORY_SERVICE_URI,
    LLM_INGRESS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane import (
    environment_service,
    history_service,
    kubernetes_account_ref,
    launcher_relay,
    llm_ingress,
    notifications,
    platform_routes,
    runner_gateway,
    staging,
)
from cluster.cdk8s.agentplane.egress import (
    BASIC_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    KUBERNETES_HOST,
    PROXY_PORT,
    PUBLIC_INTERNET_POLICY,
    admin,
)
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


def test_egress_proxy_settings_and_credentials(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", "egress")["spec"]["template"]["spec"]
    proxy = one(container for container in pod["containers"] if container["name"] == "proxy")
    proxy_env = {entry["name"]: entry for entry in proxy["env"]}
    assert {name for name in proxy_env if name.startswith("AGENTPLANE_EGRESS_")} == {
        EgressSettings.config_file_env,
        EgressSettings.database_url_env,
    }
    database = proxy_env[EgressSettings.database_url_env]["valueFrom"]["secretKeyRef"]
    assert database == {"name": "postgres-egress", "key": "uri"}
    config = config_map_yaml(staging_manifests, "egress-go-settings", "settings.yaml")
    assert config["rules_namespace"] == staging.ENV.namespace
    assert sorted(config["allowed_service_account_namespaces"]) == sorted(
        [staging.ENV.namespace, *staging.ENV.egress.external_workload_namespaces]
    )
    assert config["admin_grpc_service_callers"] == [APP_URI]
    assert pod["automountServiceAccountToken"] is True
    assert config["local_api_server"] == {
        "host": KUBERNETES_HOST,
        "port": 443,
        "token_file": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    }


def test_staging_requires_the_tls_hop(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = find_resource(staging_manifests, "Deployment", "egress")
    proxy = one(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "proxy"
    )
    ports = {port["containerPort"] for port in proxy["ports"]}
    assert admin(staging.ENV.namespace).pod_port in ports
    settings = config_map_yaml(staging_manifests, "egress-go-settings", "settings.yaml")
    assert settings["forward"]["listen_address"] == f"0.0.0.0:{PROXY_PORT}"
    assert {method["mode"] for method in settings["forward"]["auth"]} == {"spiffe"}
    api = admin(staging.ENV.namespace)
    assert api.authority in settings["tls_passthrough_targets"]


def test_tls_hop_and_rules_add_only_bound_sandbox_readers_and_tls_ports(
    staging_manifests: list[dict[str, Any]],
) -> None:
    deployment = find_resource(staging_manifests, "Deployment", "egress")
    proxy = one(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "proxy"
    )
    assert [port["containerPort"] for port in proxy["ports"]].count(PROXY_PORT) == 1
    service = find_resource(staging_manifests, "Service", "egress")
    mtls_ports = [port for port in service["spec"]["ports"] if port["port"] == PROXY_PORT]
    assert len(mtls_ports) == 1

    role = find_resource(staging_manifests, "Role", "egress")
    sandbox_rules = [rule for rule in role["rules"] if "sandboxes" in rule["resources"]]
    assert sandbox_rules == [{"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get", "list", "watch"]}]
    # An SVID names a WorkloadCredential, and the record names the subject the proxy reads live; the
    # proxy reads no runner Pod.
    assert {"apiGroups": [GROUP], "resources": ["workloadcredentials"], "verbs": ["get"]} in role["rules"]
    assert {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get"]} in role["rules"]
    all_resources = [resource for rule in role["rules"] for resource in rule["resources"]]
    assert "pods" not in all_resources

    certificate = find_resource(staging_manifests, "Certificate", "agentplane-egress-proxy")
    assert certificate["spec"]["uris"] == ["spiffe://agentplane/egress-proxy"]
    assert "server auth" in certificate["spec"]["usages"]

    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", "egress")["spec"]
    ingress_ports = {
        int(port["port"])
        for rule in policy["ingress"]
        for port_rule in rule.get("toPorts", [])
        for port in port_rule["ports"]
    }
    assert PROXY_PORT in ingress_ports


def test_basic_policy_auto_approves_only_same_principal_session_actions(
    catalog_manifests: list[dict[str, Any]],
) -> None:
    basic = find_resource(catalog_manifests, "PolicySet", BASIC_POLICY)
    action_rules = [rule for rule in basic["spec"]["rules"] if rule["type"] == "action_auto_approve"]
    assert [rule["policy"] for rule in action_rules] == [
        {
            "type": "exact_actions",
            "actions": {
                "environments": ["get", "list", "resume"],
                "sessions": [
                    "cancel_input",
                    "create",
                    "get",
                    "interrupt_turn",
                    "observe_input",
                    "read_turns",
                    "submit_input",
                ],
            },
        }
    ]

    # Standing Identity callers use the same reviewed Action approval policy in either catalog mode.
    for account in ("claude-ai", "haku-agent"):
        binding = find_resource(catalog_manifests, "PolicyBinding", account)
        assert BASIC_POLICY in binding["spec"]["policySets"]


def test_public_internet_is_available_but_never_implicitly_granted(catalog_manifests: list[dict[str, Any]]) -> None:
    public = find_resource(catalog_manifests, "PolicySet", PUBLIC_INTERNET_POLICY)
    assert public["spec"]["rules"] == [{"type": "egress", "hosts": ["*"]}]
    assert [
        (binding["metadata"]["namespace"], binding["spec"]["subject"])
        for binding in catalog_manifests
        if binding["kind"] == "PolicyBinding" and PUBLIC_INTERNET_POLICY in binding["spec"]["policySets"]
    ] == [(staging.ENV.namespace, {"identity": {"name": "public-coder-openclaw"}})]


def test_basic_has_no_projected_workload_token_route(staging_manifests: list[dict[str, Any]]) -> None:
    """Command boxes use reviewed Identity delegation; API credentials use explicit account impersonation."""
    basic = find_resource(staging_manifests, "PolicySet", BASIC_POLICY)
    assert not any(KUBERNETES_HOST in rule.get("hosts", []) for rule in basic["spec"]["rules"])
    assert not any(
        doc["kind"] == "EgressCredential" and doc["metadata"]["name"] == "kubernetes-workload"
        for doc in staging_manifests
    )


def test_notifications_verify_svids_behind_an_opaque_tunnel(catalog_manifests: list[dict[str, Any]]) -> None:
    policy = find_resource(catalog_manifests, "PolicySet", BASIC_POLICY)
    assert all(
        notifications.service(staging.ENV.namespace).fqdn not in rule.get("hosts", [])
        for rule in policy["spec"]["rules"]
    )
    assert not any(
        item["kind"] == "EgressCredential" and item["metadata"]["name"] == "agentplane-notifications-workload"
        for item in catalog_manifests
    )
    proxy_settings = config_map_yaml(catalog_manifests, "egress-go-settings", "settings.yaml")
    endpoint = notifications.service(staging.ENV.namespace)
    assert endpoint.authority in proxy_settings["tls_passthrough_targets"]
    notification_settings = config_map_yaml(catalog_manifests, f"{notifications.NAME}-settings", "settings.yaml")
    assert notification_settings["rpc"]["listen_address"] == f"0.0.0.0:{endpoint.pod_port}"
    assert notification_settings["rpc"]["tls"]["cert_file"].endswith("/tls.crt")


def test_llm_runner_traffic_uses_its_svid_listener(staging_manifests: list[dict[str, Any]]) -> None:
    llm_service = find_resource(staging_manifests, "Service", "llm-ingress")
    assert {port["port"] for port in llm_service["spec"]["ports"]} == {8080}

    settings = config_map_yaml(staging_manifests, "egress-go-settings", "settings.yaml")
    workload = llm_ingress.service(staging.ENV.namespace)
    assert workload.authority in settings["tls_passthrough_targets"]

    llm_settings = config_map_yaml(staging_manifests, "llm-ingress-settings", "settings.yaml")
    assert llm_settings["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert llm_settings["rpc"]["tls"]["cert_file"].endswith("/tls.crt")
    assert llm_settings["rpc"]["tls"]["key_file"].endswith("/tls.key")
    assert {mode["mode"] for mode in llm_settings["rpc"]["auth"]} == {"spiffe"}
    assert llm_settings["central_sampler_services"] == [CENTRAL_SAMPLER_URI]
    assert llm_settings["central_sampler_identity_callers"] is True

    certificate = find_resource(staging_manifests, "Certificate", "llm-ingress-workload")
    assert certificate["spec"]["uris"] == [LLM_INGRESS_URI]
    assert "server auth" in certificate["spec"]["usages"]

    role = find_resource(staging_manifests, "Role", "llm-ingress-sandbox-reader")
    assert role["rules"] == [
        {"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get"]},
        {"apiGroups": [GROUP], "resources": ["workloadcredentials"], "verbs": ["get"]},
        {"apiGroups": [GROUP], "resources": ["identities"], "verbs": ["get"]},
    ]

    basic_policy = find_resource(staging_manifests, "PolicySet", BASIC_POLICY)
    allowed_hosts = [host for rule in basic_policy["spec"]["rules"] for host in rule.get("hosts", [])]
    assert llm_ingress.service(staging.ENV.namespace).fqdn not in allowed_hosts

    network_policy = find_resource(staging_manifests, "CiliumNetworkPolicy", "llm-ingress")["spec"]
    assert any(
        int(port["port"]) == workload.pod_port
        for rule in network_policy["ingress"]
        for port_rule in rule.get("toPorts", [])
        for port in port_rule["ports"]
    )


def test_managed_cli_service_origins_use_svid_routes(staging_manifests: list[dict[str, Any]]) -> None:
    """Keep managed CLI origins credential-free while the sidecar presents its Sandbox SVID."""
    namespace = staging.ENV.namespace
    sessions = runner_gateway.sessions_service(namespace)
    sessions_authority = sessions.authority
    legacy_authority = f"session-dispatcher.{namespace}.svc.cluster.local:8080"
    history = history_service.service(namespace)
    history_origin = history.authority
    listener_url = platform_routes.listener_url()
    environments = environment_service.environments_service(namespace)
    basic = find_resource(staging_manifests, "PolicySet", BASIC_POLICY)
    assert all(sessions.fqdn not in rule.get("hosts", []) for rule in basic["spec"]["rules"])
    proxy_settings = config_map_yaml(staging_manifests, "egress-go-settings", "settings.yaml")
    assert {sessions_authority, history.authority, environments.authority} <= set(
        proxy_settings["tls_passthrough_targets"]
    )

    session_routes: list[dict[str, Any]] = []
    history_configured: list[dict[str, Any]] = []
    environments_configured: list[dict[str, Any]] = []
    for item in staging_manifests:
        if item["kind"] != "ConfigMap" or "settings.yaml" not in item.get("data", {}):
            continue
        settings = yaml.safe_load(item["data"]["settings.yaml"])
        routes = settings.get("workload_routes", {})
        assert legacy_authority not in routes
        assert history_origin not in routes, "Runtime's History ingress already sends opaque TLS"
        if sessions_authority in routes:
            session_routes.append(routes[sessions_authority])
        platform = settings.get("platform_routes", {}).get("routes", {})
        if "environments" in platform:
            environments_configured.append(platform["environments"])
        if "history" in platform:
            history_configured.append(platform["history"])

    assert session_routes
    for route in session_routes:
        assert route["upstream"] == sessions_authority
        assert route["server_uri"] == SESSION_DISPATCHER_URI

    assert history_configured
    for route in history_configured:
        assert route["service_prefixes"] == ["/agentplane.history_service.HistoryService/"]
        assert route["upstream"] == history.authority
        assert route["transport"] == {"mode": "tls_connect", "server_uri": HISTORY_SERVICE_URI}

    assert environments_configured
    for route in environments_configured:
        assert route["service_prefixes"] == ["/agentplane.environments.v1.EnvironmentService/"]
        assert route["upstream"] == environments.authority
        assert route["transport"] == {"mode": "tls_connect", "server_uri": SANDBOX_CONTROLLER_URI}

    pod_template = find_resource(staging_manifests, "SandboxTemplate", "environment-tools")
    pod_runner = one(
        container
        for container in pod_template["spec"]["pod"]["spec"]["containers"]
        if container["name"] == "environment-runtime"
    )
    pod_env = {entry["name"]: entry["value"] for entry in pod_runner["env"] if "value" in entry}
    assert pod_env["AGENTPLANE_SESSIONS_URL"] == f"http://{sessions_authority}"
    assert pod_env["AGENTPLANE_HISTORY_URL"] == listener_url
    assert pod_env["AGENTPLANE_ENVIRONMENTS_URL"] == listener_url
    assert not any(name.startswith("AGENTPLANE_") and name.endswith("_CREDENTIAL") for name in pod_env)
    assert all(
        source.get("serviceAccountToken", {}).get("audience") != "session-dispatcher"
        for volume in pod_template["spec"]["pod"]["spec"]["volumes"]
        for source in volume.get("projected", {}).get("sources", [])
    )
    assert all(
        source.get("serviceAccountToken", {}).get("audience") != "agentplane-history-service"
        for volume in pod_template["spec"]["pod"]["spec"]["volumes"]
        for source in volume.get("projected", {}).get("sources", [])
    )

    vm_runtime = json.loads(
        find_resource(staging_manifests, "ConfigMap", "environment-vm-config")["data"]["environment-runtime.json"]
    )
    assert vm_runtime["harness_environment"]["values"]["AGENTPLANE_SESSIONS_URL"] == f"http://{sessions_authority}"
    assert vm_runtime["harness_environment"]["values"]["AGENTPLANE_HISTORY_URL"] == (
        platform_routes.listener_url(host=launcher_relay.GUEST_GATEWAY)
    )

    assert vm_runtime["harness_environment"]["values"]["AGENTPLANE_ENVIRONMENTS_URL"] == (
        platform_routes.listener_url(host=launcher_relay.GUEST_GATEWAY)
    )


def test_github_policy_admits_the_write_verbs_a_pr_lifecycle_needs(staging_manifests: list[dict[str, Any]]) -> None:
    """The bot can finish a pull request, not only open one.

    GitHub answers a PR description with PATCH and `update-branch`/`merge` with PUT; a review comment
    or a merged head branch sits behind DELETE. POST alone leaves a sandbox able to file a change and
    unable to correct it. Pinned in both directions so that trimming a verb stays a decision rather
    than a side effect of tidying, and so the rule stays unpathed rather than narrowing quietly.
    """
    github = find_resource(staging_manifests, "PolicySet", GITHUB_AGENTYDRAGON_AGENT_POLICY)
    rule = one(github["spec"]["rules"])
    assert set(rule["methods"]) == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert rule["credentialRef"] == {"name": "github-pat"}
    # Unpathed: the verb list bounds the method, while the PAT behind it stays whole (egress.py).
    assert "paths" not in rule


def test_staging_launch_presets_bind_identity_compatible_rules_and_keep_account_grants(
    staging_manifests: list[dict[str, Any]],
) -> None:
    """New Sandbox Identities use account egress, while their existing account grants retain preset RBAC."""
    assert staging.ENV.app is not None
    presets = staging.ENV.app.config.sandbox_presets
    policies = {
        doc["metadata"]["name"]: doc["spec"]["rules"] for doc in staging_manifests if doc["kind"] == "PolicySet"
    }
    account_grants = {
        (grant["spec"]["serviceAccount"]["namespace"], grant["spec"]["serviceAccount"]["name"]): set(
            grant["spec"]["roleSets"]
        )
        for grant in staging_manifests
        if grant["kind"] == "ServiceAccountGrant"
    }

    for preset, spec in presets.items():
        launch_policies = spec.policy_sets
        assert all(not rule["type"].startswith("kubernetes_") for name in launch_policies for rule in policies[name]), (
            preset
        )
        assert all(
            rule.get("credentialRef", {}).get("name") != "kubernetes-workload"
            for name in launch_policies
            for rule in policies[name]
        ), preset

    public_role_sets = {"shared-diagnostics", "public-inventory", "cluster-diagnostics"}
    expected_role_sets = {
        "public-coder": public_role_sets,
        "finance-agent": public_role_sets
        | {
            agent_access_profiles.COINBASE_POLICY,
            agent_access_profiles.OVH_BILLING_POLICY,
            "spend-policy-config",
            agent_access_profiles.PLAID_DIAGNOSTICS_POLICY,
        },
        "haku": {
            "shared-diagnostics",
            "haku-sandbox-write",
            "cluster-diagnostics",
            agent_access_profiles.COINBASE_POLICY,
            agent_access_profiles.OVH_BILLING_POLICY,
            agent_access_profiles.PLAID_DIAGNOSTICS_POLICY,
        },
    }
    for preset, role_sets in expected_role_sets.items():
        ref = kubernetes_account_ref.account(staging.ENV.namespace, preset)
        assert ref.credential_name in presets[preset].policy_sets
        assert account_grants[(ref.namespace, ref.name)] == role_sets

    for name in (
        agent_access_profiles.COINBASE_POLICY,
        agent_access_profiles.OVH_BILLING_POLICY,
        agent_access_profiles.PLAID_DIAGNOSTICS_POLICY,
    ):
        assert any(rule["type"].startswith("kubernetes_") for rule in policies[name])
        identity_rules = policies[f"{name}-identity"]
        assert identity_rules
        assert all(not rule["type"].startswith("kubernetes_") for rule in identity_rules)
        assert identity_rules == [rule for rule in policies[name] if rule["type"] == "egress"]


def test_ovh_billing_is_a_named_read_only_credential_with_scoped_egress(
    staging_manifests: list[dict[str, Any]],
) -> None:
    secret = find_resource(staging_manifests, "ExternalSecret", "ovh-billing-credentials")
    assert {item["secretKey"] for item in secret["spec"]["data"]} == {
        "application_key",
        "application_secret",
        "consumer_key",
    }
    role = find_resource(staging_manifests, "Role", "ovh-billing-reader")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["ovh-billing-credentials"], "verbs": ["get"]}
    ]
    policy = find_resource(staging_manifests, "PolicySet", agent_access_profiles.OVH_BILLING_POLICY)
    egress = one(rule for rule in policy["spec"]["rules"] if rule["type"] == "egress")
    assert egress["hosts"] == ["api.us.ovhcloud.com"]
    assert egress["methods"] == ["GET"]
    assert set(egress["paths"]) == {
        "/1.0/auth/time",
        "/1.0/me/bill",
        "/1.0/me/bill/**",
        "/1.0/service",
        "/1.0/service/**",
    }
    assert "credentialRef" not in egress


def test_coinbase_egress_and_standing_identity_selections(staging_manifests: list[dict[str, Any]]) -> None:
    for caller in ("claude-ai", "haku-agent"):
        selected = find_resource(staging_manifests, "PolicyBinding", caller)["spec"]["policySets"]
        assert {"coinbase-identity", "plaid-diagnostics-identity"} <= set(selected)
    get_coinbase = {"type": "egress", "hosts": ["api.coinbase.com"], "methods": ["GET"]}
    assert find_resource(staging_manifests, "PolicySet", "coinbase-identity")["spec"]["rules"] == [get_coinbase]
    mixed = find_resource(staging_manifests, "PolicySet", "coinbase")["spec"]["rules"]
    assert len(mixed) == 2
    assert get_coinbase in mixed
    assert {
        "type": "kubernetes_role_binding",
        "role": {"kind": "Role", "name": "claude-ai-coinbase-reader"},
        "namespaces": [staging.ENV.namespace],
    } in mixed


def test_inference_is_granted_separately_from_platform_operations(catalog_manifests: list[dict[str, Any]]) -> None:
    granted = find_resource(catalog_manifests, "PolicySet", INFERENCE_EXPERIMENTS_POLICY)
    basic = find_resource(catalog_manifests, "PolicySet", BASIC_POLICY)
    for binding in (doc for doc in catalog_manifests if doc["kind"] == "PolicyBinding"):
        policies = binding["spec"]["policySets"]
        if BASIC_POLICY in policies:
            assert INFERENCE_EXPERIMENTS_POLICY in policies
    inference_hosts = {host for rule in granted["spec"]["rules"] for host in rule["hosts"]}
    assert inference_hosts.isdisjoint(host for rule in basic["spec"]["rules"] for host in rule.get("hosts", []))
    for name, denied_paths in (
        ("ollama", {"/api/pull", "/api/push", "/api/create", "/api/delete", "/api/copy", "/api/blobs/*"}),
        ("litellm-cheap-experiments", {"/key/generate", "/key/info", "/user/new", "/config/update"}),
    ):
        rules = [rule for rule in granted["spec"]["rules"] if rule.get("credentialRef") == {"name": name}]
        assert rules
        assert all(rule["clusterInternal"] and rule["paths"] for rule in rules)
        assert all("*" not in path for rule in rules for path in rule["paths"])
        assert denied_paths.isdisjoint(path for rule in rules for path in rule["paths"])
        assert any("POST" in rule["methods"] and "/v1/chat/completions" in rule["paths"] for rule in rules)
        assert all(set(rule["methods"]) <= {"GET", "POST"} for rule in rules)
        credential = find_resource(catalog_manifests, "EgressCredential", name)
        source = credential["spec"]["source"]["secretRef"]
        secret = find_resource(catalog_manifests, "ExternalSecret", source["name"])
        assert secret["metadata"]["namespace"] == f"{staging.ENV.namespace}-egress-credentials"
        assert source["key"] in {item["secretKey"] for item in secret["spec"]["data"]}


if __name__ == "__main__":
    pytest_bazel.main()
