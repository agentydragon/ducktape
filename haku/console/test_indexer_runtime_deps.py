"""Runtime-dependency smoke test for the packaged Haku indexer."""

from pathlib import Path

import pytest_bazel
from pydantic import SecretStr

from haku.console.indexer import ChunkSettings


def test_chunk_settings_can_read_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "indexer.yaml"
    config_file.write_text("recall_indexes: {}\n")

    settings = ChunkSettings(config_file=config_file, database_url=SecretStr("postgresql://db/index"))

    assert settings.recall_indexes == {}


if __name__ == "__main__":
    pytest_bazel.main()
