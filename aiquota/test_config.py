from pathlib import Path

import pytest
import pytest_bazel

from aiquota.config import load

if __name__ == "__main__":
    pytest_bazel.main()


def test_missing_configuration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"config\.toml"):
        load(tmp_path / "config.toml")


def test_loads_remote_api_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[remote_api]\nurl = "https://aiquota.test"\nbearer_token = "api-bearer"\n')

    config = load(config_path)

    assert config.remote_api.url == "https://aiquota.test"
    assert config.remote_api.bearer_token is not None
    assert config.remote_api.bearer_token.get_secret_value() == "api-bearer"
