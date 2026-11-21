"""Validation for the deploy-time Agent identity catalog."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.conftest import console_settings
from haku.console.identity.config import ConsoleConfigFile

_CONFIG = {"access_profiles": [{"id": "manual"}], "default_access_profile_id": "manual"}
# `console_settings` only stores this URL; nothing connects to it.
_DATABASE_URL = "postgresql://test:test@localhost/test"


def test_settings_loads_identity_catalog(tmp_path) -> None:
    config_file = tmp_path / "console.yaml"
    config_file.write_text(
        """\
access_profiles:
  - id: manual
default_access_profile_id: manual
""",
        encoding="utf-8",
    )

    settings = console_settings(_DATABASE_URL, tmp_path, config_file=config_file)

    assert settings.access_profiles[0].id == "manual"
    assert settings.default_access_profile_id == "manual"


def test_profile_catalog_rejects_duplicates_and_missing_default() -> None:
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "access_profiles": [{"id": "manual"}, {"id": "manual"}]})
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate({**_CONFIG, "default_access_profile_id": "missing"})


def test_settings_rejects_unrecognized_yaml_fields(tmp_path) -> None:
    config_file = tmp_path / "console.yaml"
    config_file.write_text(
        "access_profiles:\n  - id: manual\ndefault_access_profile_id: manual\nprofile_typo: value\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        console_settings(_DATABASE_URL, tmp_path, config_file=config_file)


if __name__ == "__main__":
    pytest_bazel.main()
