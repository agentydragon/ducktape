"""Contracts between Haku's deployed configuration and its Kubernetes wiring."""

import pytest
import pytest_bazel
import yaml
from more_itertools import one
from pydantic import SecretStr

from haku.console.channels.matrix.config import AdapterConfigFile
from haku.console.channels.matrix.worker import AdapterSettings, _launch_wiring
from haku.console.config import ClaudeCodeImplementationConfig, OperatorIdentityConfig, OperatorOidcConfig
from haku.console.indexer import ChunkSettings, EmbedSettings, IndexerRole
from haku.console.indexer_config import IndexerConfigFile
from haku.console.mcp_config import PreregisteredOAuthClient, RemoteMcpBackend, RemoteServerOAuthAuth
from haku.console.settings import Settings
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
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
        "HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_ID",
        "HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_SECRET",
        "HAKU_CONSOLE__EGRESS_DECIDE__DECISION_ENDPOINT_TOKEN",
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

    # The deployed ConfigMap writes the canonical `harnesses` key.
    assert "harnesses" in raw
    assert config.harnesses is not None
    claude = config.harnesses.claude_code
    assert claude.claim_prefix == "claude"
    assert claude.harness_label == "claude"
    assert isinstance(claude.implementation, ClaudeCodeImplementationConfig)
    codex = config.harnesses.codex_app_server
    assert codex is not None
    assert codex.claim_prefix == "codex"
    assert codex.harness_label == "codex"
    assert isinstance(codex.implementation, CodexAppServerImplementationConfig)
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
    assert profiles["haku"].in_process_server_ids == {
        "haku_conversations",
        "grants",
        "sandbox",
        "workers",
        "haku_session_sandboxes",
    }
    assert "haku_session_sandboxes" in profiles["public-coder"].in_process_server_ids

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
    for root in ("haku_v1", "public_coder_safe_reads"):
        assert "kubernetes_reads" in policies[root]["policies"], root
        assert "grants_self_introspection" in policies[root]["policies"], root
        assert "grants_own_revoke" in policies[root]["policies"], root
    assert all(
        "haku_session_sandboxes" not in (policy.get("server"), *policy.get("tools", {}))
        for policy in raw["auto_approval_policies"]
    )

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

    # A configuration grant's named credential must actually redeem what it admits — the decide
    # service otherwise skips substitution with only a warning, and the fenced workload's inert
    # placeholder goes upstream and is rejected there (#4941/#4943).
    egress = config.egress_decide
    assert egress is not None
    registry = {credential.handle: credential for credential in egress.credentials.values()}
    for entry in egress.grants:
        if entry.credential_handle is None:
            continue
        credential = registry[entry.credential_handle]
        assert entry.principal == credential.principal, entry.id
        assert entry.origins <= credential.origins, entry.id


def test_deployed_egress_decide_env_slots_are_bound_at_their_rigor() -> None:
    """Every env slot `egress_decide` names must resolve in the server container, at the rigor
    the typed model assigns it: the fence credential identity slot fails loud at
    startup, so they are non-optional Secret references; a registry credential slot may be an
    optional Secret reference (unset skips the credential with a warning, #4970) or a committed
    literal — acceptable only when inert by construction, hence the EXAMPLE- prefix. The sidecar
    presents the same fence credential, so its reference must name the same Secret key the server
    resolves."""
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    egress = config["egress_decide"]
    deployment = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/deployment.yaml").read_text())
    containers = {container["name"]: container for container in deployment["spec"]["template"]["spec"]["containers"]}
    server_env = {entry["name"]: entry for entry in containers["server"]["env"]}

    decision_slot = "HAKU_CONSOLE__EGRESS_DECIDE__DECISION_ENDPOINT_TOKEN"
    reference = server_env[decision_slot]["valueFrom"]["secretKeyRef"]
    assert not reference.get("optional", False), f"identity {decision_slot=} must fail loud, never be optional"

    for slot, credential in egress["credentials"].items():
        entry = server_env[f"HAKU_CONSOLE__EGRESS_DECIDE__CREDENTIALS__{slot.upper()}__VALUE"]
        if "value" in entry:
            assert entry["value"].startswith("EXAMPLE-"), f"literal value for {credential['handle']} must be inert"
        else:
            assert "secretKeyRef" in entry["valueFrom"], credential["handle"]

    sidecar_env = {entry["name"]: entry for entry in containers["egress-proxy"]["env"]}
    assert sidecar_env["HAKU_DECISION_ENDPOINT_TOKEN"]["valueFrom"] == server_env[decision_slot]["valueFrom"]
    assert "HAKU_EGRESS_PROXY_TOKEN" not in server_env
    assert "HAKU_EGRESS_PROXY_TOKEN" not in sidecar_env


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
    # https, never http: client-go attaches kubeconfig credentials only to a TLS server, so a
    # plain-http proxy URL silently un-authenticates every sandbox kubectl request.
    assert settings.runner_kubernetes_proxy_url == "https://haku-kube-api-proxy.haku-console.svc.cluster.local:8443"
    assert str(settings.haku_agent_workspace_setup) == "/usr/local/bin/haku-sandbox-setup.sh"
    config = settings
    assert config.harnesses is not None
    codex = config.harnesses.codex_app_server
    assert codex is not None
    implementation = codex.implementation
    assert isinstance(implementation, CodexAppServerImplementationConfig)
    assert implementation.api_base_url == "http://litellm.litellm.svc.cluster.local:4000/v1"
    assert codex.mcp_url == "http://haku-console.haku-console.svc.cluster.local:9090/mcp"
    # Codex routes through the colocated Console egress fence (#4670), not a dedicated runner proxy.
    assert codex.https_proxy == "http://haku-egress-proxy.haku-console.svc.cluster.local:8888"
    # LiteLLM stays OUT of no_proxy: its model traffic must traverse the fence for the virtual-key
    # substitution (admitted through the configuration grant's allow_prohibited_address).
    assert "litellm.litellm.svc.cluster.local" not in codex.no_proxy


def _indexer_deployment_env(filename: str, role: IndexerRole) -> dict[str, str]:
    """The literal env of the role's Deployment, checking the manifest names a role this binary has."""
    deployment = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/haku/console/{filename}").read_text())
    container = one(deployment["spec"]["template"]["spec"]["containers"])
    assert IndexerRole(one(container["args"]).removeprefix("--role=")) is role
    return {item["name"]: item["value"] for item in container["env"] if "value" in item}


def test_deployed_chunk_role_env_satisfies_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each registry index has a chunk pod that starts from exactly its manifest env.

    Derived from the deploy-owned registry rather than a fixed roster: a new `recall_indexes` entry
    with no `indexer-chunk-<id>-deployment.yaml` fails here. The contract is exactly
    {config_file, database_url} — no embedder configuration, and no index selector: the mounted
    config slice is the selection.
    """
    config = _console_settings(monkeypatch)
    for index in config.recall_indexes.values():
        env = _indexer_deployment_env(f"indexer-chunk-{index.index_id}-deployment.yaml", IndexerRole.CHUNK)
        with monkeypatch.context() as patched:
            for name, value in env.items():
                patched.setenv(name, value)
            # Point the deployment's mount path at the equivalent runfile so the settings source
            # exercises the whole projected YAML in this hermetic test.
            patched.setenv(
                "HAKU_INDEXER_CONFIG_FILE",
                str(get_required_path(f"ducktape/cluster/k8s/haku/console/indexer-chunk-{index.index_id}-config.yaml")),
            )
            # The one secret env the manifest binds by reference rather than value.
            patched.setenv("HAKU_INDEXER__DATABASE_URL", "postgresql+asyncpg://haku_indexer@db.test/approval_store")
            if index.index_id == "haku-state":
                patched.setenv("HAKU_INDEXER__RECALL_INDEXES__HAKU_STATE__CREDENTIALS__USERNAME", "haku")
                patched.setenv("HAKU_INDEXER__RECALL_INDEXES__HAKU_STATE__CREDENTIALS__PASSWORD", "secret")
            assert ChunkSettings().config_file.name == f"indexer-chunk-{index.index_id}-config.yaml"


def test_deployed_chunk_config_slices_project_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """One instance, one config slice: each pod's mounted file equals its registry projection.

    The console still reads the whole `recall_indexes` registry in config.yaml; each chunk pod
    mounts only `indexer-chunk-<id>-config.yaml`, which must parse — through the worker's own
    reader — to exactly that one registry entry plus the console's Git CA bundle. The slices are
    generated output pinned to the registry (the LiteLLM config pattern), so a registry edit that
    misses its slice, a drifted slice, or a slice grown past one entry fails here.
    """
    console = _console_settings(monkeypatch)
    for slot, index in console.recall_indexes.items():
        slice_path = get_required_path(f"ducktape/cluster/k8s/haku/console/indexer-chunk-{index.index_id}-config.yaml")
        assert IndexerConfigFile.model_validate(yaml.safe_load(slice_path.read_text())) == IndexerConfigFile(
            git_ca_bundle=console.git_ca_bundle, recall_indexes={slot: index}
        )


def test_deployed_embed_role_env_satisfies_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embed pod starts from exactly its manifest env — no registry or Git configuration required."""
    for name, value in _indexer_deployment_env("indexer-embed-deployment.yaml", IndexerRole.EMBED).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HAKU_INDEXER__DATABASE_URL", "postgresql+asyncpg://haku_indexer@db.test/approval_store")
    settings = EmbedSettings()
    assert settings.embedder.base_url.startswith("http")


def test_deployed_matrix_adapter_env_satisfies_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter pod starts from exactly its manifest env — no console settings required."""
    deployment = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/haku/console/matrix-adapter-deployment.yaml").read_text()
    )
    container = one(deployment["spec"]["template"]["spec"]["containers"])
    for item in container["env"]:
        if "value" in item:
            monkeypatch.setenv(item["name"], item["value"])
    monkeypatch.setenv(
        "HAKU_MATRIX_ADAPTER_CONFIG_FILE", str(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml"))
    )
    # The three secret envs the manifest binds by reference rather than value.
    monkeypatch.setenv(
        "HAKU_MATRIX_ADAPTER__DATABASE_URL", "postgresql+asyncpg://haku_matrix_adapter@db.test/approval_store"
    )
    monkeypatch.setenv("HAKU_MATRIX_ADAPTER__MATRIX__OPERATOR_SUBJECT", "authentik-user-id")
    monkeypatch.setenv("HAKU_MATRIX_ADAPTER__MATRIX__PASSWORD", "bot-password")
    settings = AdapterSettings()
    assert settings.config_file.name == "config.yaml"
    # The anchor namespace the operator subject resolves through is written at console login,
    # so the two Deployments must name the same trust domain.
    console = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/deployment.yaml").read_text())
    server = one(c for c in console["spec"]["template"]["spec"]["containers"] if c["name"] == "server")
    server_env = {item["name"]: item.get("value") for item in server["env"]}
    assert settings.operator_identity_trust_domain == server_env["HAKU_CONSOLE__OPERATOR_IDENTITY__TRUST_DOMAIN"]


def test_deployed_config_reads_identically_for_console_and_matrix_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two parsers, one mounted file: the adapter's launch-identity slice must agree with the console's read."""
    config_path = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml")
    console = _console_settings(monkeypatch)
    adapter = AdapterConfigFile.model_validate(yaml.safe_load(config_path.read_text()))
    assert {entry.agent_id for entry in adapter.launchable_agents} == {
        entry.agent_id for entry in console.launchable_agents
    }
    assert {profile.id: profile.allowed_harnesses for profile in adapter.access_profiles} == {
        profile.id: profile.allowed_harnesses for profile in console.access_profiles
    }
    assert {agent.agent_id: agent.access_profile_id for agent in adapter.static_agents.values()} == {
        agent.agent_id: agent.access_profile_id for agent in console.static_agents.values()
    }
    assert adapter.matrix_launch is not None
    launch = _launch_wiring(adapter)
    assert launch is not None
    assert launch.default_agent_id == adapter.matrix_launch.default_agent_id
    assert launch.harness_kind == adapter.matrix_launch.default_harness_kind
    assert console.harnesses is not None
    assert adapter.harnesses is not None
    assert adapter.harnesses.claude_code is not None
    assert adapter.harnesses.claude_code.agent_id == console.harnesses.claude_code.agent_id
    assert (adapter.harnesses.codex_app_server is None) == (console.harnesses.codex_app_server is None)
    if console.harnesses.codex_app_server is not None:
        assert adapter.harnesses.codex_app_server is not None
        assert adapter.harnesses.codex_app_server.agent_id == console.harnesses.codex_app_server.agent_id


def test_matrix_creation_route_does_not_fall_back_when_unconfigured() -> None:
    config_path = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml")
    adapter = AdapterConfigFile.model_validate(yaml.safe_load(config_path.read_text()))

    with pytest.raises(ValueError, match=r"matrix\.default_harness_kind"):
        _launch_wiring(adapter.model_copy(update={"matrix_launch": None}))


if __name__ == "__main__":
    pytest_bazel.main()
