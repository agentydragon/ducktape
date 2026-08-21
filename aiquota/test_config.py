from pathlib import Path

import pytest_bazel

from aiquota.config import load

if __name__ == "__main__":
    pytest_bazel.main()


def test_remote_companion_overrides_remote_api_without_replacing_main_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[claude]\nenabled = false\n")
    (tmp_path / "remote.toml").write_text('[remote_api]\nurl = "https://aiquota.test"\nbearer_token = "api-bearer"\n')

    config = load(config_path)

    assert config.claude.enabled is False
    assert config.remote_api.url == "https://aiquota.test"
    assert config.remote_api.bearer_token is not None
    assert config.remote_api.bearer_token.get_secret_value() == "api-bearer"
