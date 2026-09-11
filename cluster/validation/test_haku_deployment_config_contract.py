"""Contracts between Haku's deployed configuration and its Kubernetes wiring."""

from uuid import uuid4

import pytest
import pytest_bazel
import yaml
from pydantic import SecretStr

from haku.console.auto_approval.registry import AutoApprovalPolicyRegistry, ToolAutoApprovalMode
from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig
from haku.console.mcp_config import PreregisteredOAuthClient, RemoteMcpBackend, RemoteServerOAuthAuth, StaticBearerAuth
from haku.console.settings import Settings
from haku.console.tool_call_actor import AgentActor
from util.bazel.runfiles import get_required_path


def _console_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in (
        "HAKU_CONSOLE__STATIC_AGENTS__HAKU__TOKEN",
        "HAKU_CONSOLE__STATIC_AGENTS__HAKU__OPERATOR_SUBJECT",
        "HAKU_CONSOLE__STATIC_AGENTS__PUBLIC_CODER__TOKEN",
        "HAKU_CONSOLE__STATIC_AGENTS__PUBLIC_CODER__OPERATOR_SUBJECT",
        "HAKU_CONSOLE__NODE_DAEMONS__DAEMONS__WYRM2__TOKEN",
        "HAKU_CONSOLE__NODE_DAEMONS__DAEMONS__RUGGED__TOKEN",
        "HAKU_CONSOLE__NODE_DAEMONS__DAEMONS__ATLAS__TOKEN",
        "HAKU_CONSOLE__MCP__SERVERS__TANA_RW__BACKEND__AUTH__TOKEN",
        "HAKU_CONSOLE__MCP__SERVERS__HOME_ASSISTANT__BACKEND__AUTH__TOKEN",
        "HAKU_CONSOLE__MCP__SERVERS__SSH__BACKEND__AUTH__TOKEN",
        "HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_ID",
        "HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_SECRET",
    ):
        monkeypatch.setenv(name, f"test-{name.lower()}")
    return Settings(
        config_file=get_required_path("ducktape/cluster/k8s/haku/console/config.yaml"),
        haku_ui_url="https://haku-ui.test",
        auth_origin="https://auth.test",
        public_base_url="https://haku.test",
        database_url=SecretStr("postgresql+psycopg://db.test/haku"),
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.test/application/o/haku-console/",
            client_id="console",
            client_secret=SecretStr("secret"),
            session_secret=SecretStr("session-secret"),
        ),
        operator_identity=OperatorIdentityConfig(trust_domain="auth.test/authentik-user-id/v1"),
        max_wait_for_result_ms=60_000,
    )


def test_deployed_console_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_text = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text()
    raw = yaml.safe_load(raw_text)
    config = _console_settings(monkeypatch)

    assert (
        config.static_agents["haku"]
        .token.get_secret_value()
        .startswith("test-haku_console__static_agents__haku__token")
    )
    github_backend = config.mcp.servers["github"].backend
    assert isinstance(github_backend, RemoteMcpBackend)
    assert isinstance(github_backend.auth, RemoteServerOAuthAuth)
    github_registration = github_backend.auth.client_registration
    assert isinstance(github_registration, PreregisteredOAuthClient)
    assert github_registration.client_id.startswith(
        "test-haku_console__mcp__servers__github__backend__auth__client_registration__client_id"
    )

    profiles = {profile.id: profile for profile in config.access_profiles}
    assert profiles["haku"].in_process_server_ids == {"grants", "sandbox"}

    assert config.kubernetes_authorization is not None
    subjects = config.kubernetes_authorization.subjects_by_access_profile
    assert subjects["haku"].username == "haku:access-profile:haku"
    assert subjects["haku"].groups == ("haku:access-profile:haku", "system:authenticated")
    assert subjects["public-coder"].username == "haku:access-profile:public-coder"
    assert subjects["public-coder"].groups == ("haku:access-profile:public-coder", "system:authenticated")

    # Listing an in-process server and configuring what it serves are one decision recorded in two
    # places: without `agent_sandbox` nothing registers `sandbox`, and startup fails
    # `validate_in_process_server_bindings` rather than quietly offering a server that cannot run.
    server_ids = {server.id for server in config.mcp.servers.values()}
    github_server = config.mcp.servers["github"]
    assert github_server.agent_tool_denylist == {
        "assign_copilot_to_issue",
        "create_pull_request_with_copilot",
        "get_copilot_job_status",
        "request_copilot_review",
    }
    assert ("sandbox" in server_ids) == (config.agent_sandbox is not None)

    policies = {policy["id"]: policy for policy in raw["auto_approval_policies"]}
    # A policy naming a server the catalog does not declare governs nothing at all, and does so
    # silently — renaming a server would leave its approvals behind without failing anything.
    for policy in raw["auto_approval_policies"]:
        named = set(policy["tools"]) if policy["type"] == "exact_tools" else set()
        if (server := policy.get("server")) is not None:
            named.add(server)
        assert named <= server_ids, policy["id"]
    # The unconditional grant reads: kubernetes SAR inspection and own-scoped get_grant.
    assert policies["kubernetes_reads"]["tools"] == {"grants": ["kubernetes_can_i", "get_grant"]}
    # An Agent's own-grant list read is auto-approved only for the explicit `principal=self` scope.
    assert policies["grants_own_list"] == {"id": "grants_own_list", "type": "grant_self_list", "server": "grants"}
    # whoami is an argument-free, side-effect-free identity read (the caller's own resolved
    # console/MCP principal), so it is unconditionally auto-approvable — its own exact-tools atom.
    assert policies["grants_whoami"]["tools"] == {"grants": ["whoami"]}
    # The two self-reads are bundled into one any_of so each root references it once (DRY) instead of
    # repeating the pair; both atoms stay individually defined above.
    assert policies["grants_self_introspection"]["type"] == "any_of"
    assert set(policies["grants_self_introspection"]["policies"]) == {"grants_whoami", "grants_own_list"}
    # An Agent's revoke_grants only ever relinquishes its OWN grants (the tool filters to the caller;
    # owner_agent_id is operator-only and rejected for an Agent), so it is a narrowing self-service
    # operation and click-free — its own exact-tools atom, distinct from the widening create_grant.
    assert policies["grants_own_revoke"]["type"] == "exact_tools"
    assert policies["grants_own_revoke"]["tools"] == {"grants": ["revoke_grants"]}
    for root in ("haku_v1", "public_coder_v1"):
        assert "kubernetes_reads" in policies[root]["policies"], root
        assert "grants_self_introspection" in policies[root]["policies"], root
        assert "grants_own_revoke" in policies[root]["policies"], root

    # Every Agent may ASK for a grant: the unified `grants` server is exposed to every access profile
    # (operator ruling on #4986). Safe only together with the pin below — nothing in it auto-approves.
    for profile in config.access_profiles:
        assert "grants" in profile.in_process_server_ids, profile.id

    # An auto-approved source ToolCall cannot mint a grant (the repository's provenance check
    # requires approval_policy_id absent), so auto-approving create_grant would make every grant
    # creation fail after the fact instead of queueing for the Operator. create_grant (widening —
    # it issues new temporary authority) therefore never auto-approves in any policy. revoke_grants
    # (narrowing — an Agent relinquishes only its own grants) is click-free, but ONLY through the
    # dedicated grants_own_revoke atom; no other exact-tools policy may smuggle either verb in.
    for policy in raw["auto_approval_policies"]:
        if policy["type"] != "exact_tools":
            continue
        grant_tools = policy["tools"].get("grants", [])
        assert "create_grant" not in grant_tools, policy["id"]
        if policy["id"] != "grants_own_revoke":
            assert "revoke_grants" not in grant_tools, policy["id"]


def test_ssh_backend_uses_shared_secret_and_requires_agent_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _console_settings(monkeypatch)
    backend = config.mcp.servers["ssh"].backend
    assert isinstance(backend, RemoteMcpBackend)
    assert isinstance(backend.auth, StaticBearerAuth)
    assert backend.auth.token.get_secret_value().startswith(
        "test-haku_console__mcp__servers__ssh__backend__auth__token"
    )
    deployment = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/deployment.yaml").read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    reference = next(
        e for e in container["env"] if e["name"] == "HAKU_CONSOLE__MCP__SERVERS__SSH__BACKEND__AUTH__TOKEN"
    )["valueFrom"]["secretKeyRef"]
    source = next(
        r
        for r in yaml.safe_load_all(get_required_path("_main/cluster/k8s/ssh-mcp/secrets/bearer-eso.yaml").read_text())
        if r["kind"] == "ExternalSecret"
    )
    assert reference["name"] == source["spec"]["target"]["name"]
    assert reference["key"] in source["spec"]["target"]["template"]["data"]
    assert reference["name"] in deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"].split(",")
    staging = yaml.safe_load(
        get_required_path("_main/cluster/k8s/agentplane-staging/actions/settings.yaml").read_text()
    )
    assert str(backend.url) == staging["action_groups"]["ssh"]["executor"]["config"]["url"]
    registry = AutoApprovalPolicyRegistry(config)
    for profile in config.access_profiles:
        actor = AgentActor(agent_id=uuid4(), operator_id=uuid4(), binding_id=uuid4(), access_profile_id=profile.id)
        for tool in ("list_targets", "exec"):
            assert registry.tool_mode(actor, "ssh", tool) is ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED


def test_deployed_console_settings_load_from_the_shared_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml")
    deployment = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/deployment.yaml").read_text())
    container_env = {
        item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    max_wait_for_result_ms = container_env["HAKU_CONSOLE__MAX_WAIT_FOR_RESULT_MS"]
    assert max_wait_for_result_ms is not None
    monkeypatch.setenv("HAKU_CONSOLE_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("HAKU_CONSOLE__MAX_WAIT_FOR_RESULT_MS", max_wait_for_result_ms)
    settings = _console_settings(monkeypatch)

    assert settings.config_file == config_path
    assert settings.max_wait_for_result_ms == int(max_wait_for_result_ms)


if __name__ == "__main__":
    pytest_bazel.main()
