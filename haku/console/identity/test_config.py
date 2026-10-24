"""Validation for the deploy-time Agent identity catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import SecretStr, ValidationError

from haku.console.identity.config import ConsoleConfigFile
from haku.console.settings import Settings

_CONFIG = {"access_profiles": [{"id": "manual"}], "default_access_profile_id": "manual"}


def _settings_from_yaml(config_file: Path) -> Settings:
    return Settings(
        config_file=config_file,
        haku_ui_url="https://haku-ui.test",
        auth_origin="https://auth.test",
        public_base_url="https://haku.test",
        database_url=SecretStr("postgresql://test:test@localhost/test"),
        operator_oidc={
            "issuer": "https://auth.test/application/o/haku-console/",
            "client_id": "console",
            "client_secret": "secret",
            "session_secret": "secret",
        },
        operator_identity={"trust_domain": "auth.test/authentik-user-id/v2"},
    )


def test_retired_tool_call_yaml_fields_are_ignored_by_settings_during_rollout(tmp_path) -> None:
    config_file = tmp_path / "console.yaml"
    config_file.write_text(
        """\
access_profiles:
  - id: manual
    auto_approval_policy: manual
    in_process_server_ids: [gmail]
default_access_profile_id: manual
static_agents:
  haku:
    agent_id: 00000000-0000-0000-0000-000000000002
    display_name: Haku
    token: retired-token
    operator_subject: retired-operator
    access_profile_id: manual
auto_approval_policies:
  - id: manual
    type: never
mcp:
  servers: {}
""",
        encoding="utf-8",
    )

    settings = _settings_from_yaml(config_file)

    assert settings.access_profiles[0].id == "manual"
    assert not hasattr(settings, "auto_approval_policies")
    assert not hasattr(settings, "mcp")
    assert not hasattr(settings, "static_agents")


def test_profile_catalog_rejects_duplicates_and_missing_default() -> None:
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "access_profiles": [{"id": "manual"}, {"id": "manual"}]})
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "default_access_profile_id": "missing"})


def test_profile_and_catalog_still_reject_unrecognized_fields() -> None:
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "auto_approval_policy_typo": "manual"})
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "access_profiles": [{"id": "manual", "unexpected": True}]})


def test_settings_rejects_unrecognized_yaml_fields(tmp_path) -> None:
    config_file = tmp_path / "console.yaml"
    config_file.write_text(
        "access_profiles:\n  - id: manual\ndefault_access_profile_id: manual\nprofile_typo: value\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        _settings_from_yaml(config_file)


if __name__ == "__main__":
    pytest_bazel.main()
