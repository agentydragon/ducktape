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

    profiles = {profile.id: profile for profile in config.access_profiles}
    assert profiles["haku"].in_process_server_ids == {"haku_conversations", "kubernetes"}

    assert config.kubernetes_authorization is not None
    subjects = config.kubernetes_authorization.subjects_by_access_profile
    assert subjects["haku"].username == "haku:access-profile:haku"
    assert subjects["haku"].groups == ("haku:access-profile:haku", "system:authenticated")
    assert subjects["public-coder"].username == "haku:access-profile:public-coder"
    assert subjects["public-coder"].groups == ("haku:access-profile:public-coder", "system:authenticated")

    policies = {policy["id"]: policy for policy in raw["auto_approval_policies"]}
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
