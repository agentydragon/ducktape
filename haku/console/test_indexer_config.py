"""The worker parses only its slice of the shared console config file."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_bazel
from more_itertools import one
from pydantic import SecretStr

from haku.console.indexer import ChunkSettings
from haku.recall_index.config import GitRecallIndexDefinition


def test_reads_registry_and_ca_bundle_ignoring_console_only_siblings(tmp_path: Path) -> None:
    """Console-only sections — including vocabularies this image has no word for — never break the parse."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            mcp:
              servers: {}
            access_profiles:
              - id: haku
                unknown_future_field: true
            auto_approval_policies:
              - {id: nonsense, type: policy_kind_from_a_newer_release}
            git_ca_bundle: /etc/certs/custom-ca.crt
            recall_indexes:
              ducktape:
                index_id: ducktape
                index_type: git
                repo_url: https://example.test/ducktape.git
                branch: devel
            """
        )
    )
    config = ChunkSettings(config_file=config_file, database_url=SecretStr("postgresql://db/index"))
    assert config.git_ca_bundle == Path("/etc/certs/custom-ca.crt")
    index = one(config.recall_indexes.values())
    assert isinstance(index, GitRecallIndexDefinition)
    assert (index.index_id, index.repo_url, index.branch) == ("ducktape", "https://example.test/ducktape.git", "devel")


def test_empty_file_yields_empty_registry_and_system_trust(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")
    config = ChunkSettings(config_file=config_file, database_url=SecretStr("postgresql://db/index"))
    assert config.recall_indexes == {}
    assert config.git_ca_bundle == Path("/etc/ssl/certs/ca-certificates.crt")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        ChunkSettings(config_file=tmp_path / "absent.yaml", database_url=SecretStr("postgresql://db/index"))


if __name__ == "__main__":
    pytest_bazel.main()
