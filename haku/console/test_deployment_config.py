"""Contracts for the deploy-owned Haku Console configuration."""

import pytest
import pytest_bazel
import yaml
from pydantic import SecretStr

from haku.console.config import OperatorIdentityConfig, OperatorOidcConfig, Settings
from haku.console.mcp_config import ConsoleConfigFile
from util.bazel.runfiles import get_required_path


def test_deployed_console_config_is_valid() -> None:
    raw = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    config = ConsoleConfigFile.model_validate(raw)

    # Keep the shared wire shape readable by previous replicas until the schema cutover.
    claude = raw["chat_runtimes"]["claude_code"]
    assert "implementation" not in claude
    assert {"oauth_placeholder", "mcp_static_agent_id"} <= claude.keys()

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
    monkeypatch.setenv("HAKU_CONSOLE_CONFIG_FILE", str(config_path))
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
    assert settings.runner_kubernetes_proxy_url == "http://haku-kube-api-proxy.haku-console.svc.cluster.local:8080"
    assert str(settings.haku_agent_workspace_setup) == "/usr/local/bin/haku-sandbox-setup.sh"


if __name__ == "__main__":
    pytest_bazel.main()
