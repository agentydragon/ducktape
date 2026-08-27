"""Contracts for the deploy-owned Haku Console configuration."""

import pytest
import pytest_bazel
import yaml
from more_itertools import one
from pydantic import SecretStr

from haku.console.config import ClaudeCodeImplementationConfig, OperatorIdentityConfig, OperatorOidcConfig, Settings
from haku.console.indexer import ChunkSettings, EmbedSettings, IndexerRole
from haku.console.indexer_config import load_indexer_config
from haku.console.mcp_config import ConsoleConfigFile
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
from util.bazel.runfiles import get_required_path


def test_deployed_console_config_is_valid() -> None:
    raw = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    config = ConsoleConfigFile.model_validate(raw)

    # The deployed ConfigMap still writes the deprecated `chat_runtimes` key (#4772 C4c expand);
    # the loader maps it onto the canonical `harnesses` field.
    assert "chat_runtimes" in raw
    assert config.harnesses is not None
    claude = config.harnesses.claude_code
    assert claude.claim_prefix == "claude"
    assert claude.runtime_label == "claude-chat"
    assert isinstance(claude.implementation, ClaudeCodeImplementationConfig)
    codex = config.harnesses.codex_app_server
    assert codex is not None
    assert codex.claim_prefix == "codex"
    assert codex.runtime_label == "codex-chat"
    assert isinstance(codex.implementation, CodexAppServerImplementationConfig)
    assert "codex_runtime" not in raw["settings"]

    profiles = {profile.id: profile for profile in config.access_profiles}
    assert profiles["haku"].in_process_server_ids == {"haku_conversations", "kubernetes", "sandbox"}

    assert config.kubernetes_authorization is not None
    subjects = config.kubernetes_authorization.subjects_by_access_profile
    assert subjects["haku"].username == "haku:access-profile:haku"
    assert subjects["haku"].groups == ("haku:access-profile:haku", "system:authenticated")
    assert subjects["public-coder"].username == "haku:access-profile:public-coder"
    assert subjects["public-coder"].groups == ("haku:access-profile:public-coder", "system:authenticated")

    # Listing an in-process server and configuring what it serves are one decision recorded in two
    # places: without `agent_sandbox` nothing registers `sandbox`, and startup fails
    # `validate_in_process_server_bindings` rather than quietly offering a server that cannot run.
    server_ids = {server.id for server in config.mcp.servers}
    assert ("sandbox" in server_ids) == (config.agent_sandbox is not None)

    policies = {policy["id"]: policy for policy in raw["auto_approval_policies"]}
    # A policy naming a server the catalog does not declare governs nothing at all, and does so
    # silently — renaming a server would leave its approvals behind without failing anything.
    for policy in raw["auto_approval_policies"]:
        named = set(policy["tools"]) if policy["type"] == "exact_tools" else set()
        if (server := policy.get("server")) is not None:
            named.add(server)
        assert named <= server_ids, policy["id"]
    assert policies["kubernetes_reads"]["tools"] == {"kubernetes": ["can_i", "list_grants", "get_grant"]}
    assert "kubernetes_reads" in policies["haku_v1"]["policies"]
    assert "kubernetes_reads" in policies["public_coder_safe_reads"]["policies"]


def test_deployed_console_settings_load_from_the_shared_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml")
    deployment = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/deployment.yaml").read_text())
    container_env = {
        item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    max_wait_for_result_ms = container_env["HAKU_CONSOLE_MAX_WAIT_FOR_RESULT_MS"]
    assert max_wait_for_result_ms is not None
    monkeypatch.setenv("HAKU_CONSOLE_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("HAKU_CONSOLE_MAX_WAIT_FOR_RESULT_MS", max_wait_for_result_ms)
    settings = Settings(
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
    )

    assert settings.config_file == config_path
    assert settings.max_wait_for_result_ms == int(max_wait_for_result_ms)
    # https, never http: client-go attaches kubeconfig credentials only to a TLS server, so a
    # plain-http proxy URL silently un-authenticates every sandbox kubectl request.
    assert settings.runner_kubernetes_proxy_url == "https://haku-kube-api-proxy.haku-console.svc.cluster.local:8443"
    assert str(settings.haku_agent_workspace_setup) == "/usr/local/bin/haku-sandbox-setup.sh"
    raw = yaml.safe_load(config_path.read_text())
    config = ConsoleConfigFile.model_validate(raw)
    assert config.harnesses is not None
    codex = config.harnesses.codex_app_server
    assert codex is not None
    implementation = codex.implementation
    assert isinstance(implementation, CodexAppServerImplementationConfig)
    assert implementation.api_base_url == "http://litellm.litellm.svc.cluster.local:4000/v1"
    assert codex.mcp_url == "http://haku-console.haku-console.svc.cluster.local:9090/mcp"
    assert codex.https_proxy == ("http://public-coder-codex-runner-proxy.public-coder-agent.svc.cluster.local:8080")
    assert "litellm.litellm.svc.cluster.local" not in codex.no_proxy


def _indexer_deployment_env(filename: str, role: IndexerRole) -> dict[str, str]:
    """The literal env of the role's Deployment, checking the manifest names a role this binary has."""
    deployment = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/haku/console/{filename}").read_text())
    container = one(deployment["spec"]["template"]["spec"]["containers"])
    assert IndexerRole(one(container["args"]).removeprefix("--role=")) is role
    return {item["name"]: item["value"] for item in container["env"] if "value" in item}


def test_deployed_chunk_role_env_satisfies_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chunk pod starts from exactly its manifest env — no embedder configuration required."""
    for name, value in _indexer_deployment_env("indexer-deployment.yaml", IndexerRole.CHUNK).items():
        monkeypatch.setenv(name, value)
    # The one secret env the manifest binds by reference rather than value.
    monkeypatch.setenv("HAKU_INDEXER_DATABASE_URL", "postgresql+asyncpg://haku_indexer@db.test/approval_store")
    assert ChunkSettings().config_file.name == "config.yaml"


def test_deployed_config_reads_identically_for_console_and_indexer() -> None:
    """Two parsers, one mounted file: the worker's narrow slice must agree with the console's read."""
    config_path = get_required_path("ducktape/cluster/k8s/haku/console/config.yaml")
    console = ConsoleConfigFile.model_validate(yaml.safe_load(config_path.read_text()))
    indexer = load_indexer_config(config_path)
    assert indexer.recall_indexes == console.recall_indexes
    assert indexer.git_ca_bundle == console.git_ca_bundle


def test_deployed_embed_role_env_satisfies_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embed pod starts from exactly its manifest env — no registry or Git configuration required."""
    for name, value in _indexer_deployment_env("indexer-embed-deployment.yaml", IndexerRole.EMBED).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HAKU_INDEXER_DATABASE_URL", "postgresql+asyncpg://haku_indexer@db.test/approval_store")
    settings = EmbedSettings()
    assert settings.embedder.base_url.startswith("http")


if __name__ == "__main__":
    pytest_bazel.main()
