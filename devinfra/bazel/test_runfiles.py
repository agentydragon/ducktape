"""Tests for the shared Bazel runfiles helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_bazel
from python.runfiles import runfiles as rules_runfiles

from devinfra.bazel import runfiles


def _synthetic_runfiles(tmp_path: Path, files: dict[str, str], mappings: list[str]) -> rules_runfiles.Runfiles:
    root = tmp_path / "runfiles"
    for runfile, content in files.items():
        path = root / runfile
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    repo_mapping = root / "_repo_mapping"
    if mappings:
        repo_mapping.parent.mkdir(parents=True, exist_ok=True)
        repo_mapping.write_text("\n".join(mappings) + "\n")
    result = rules_runfiles.Create({"RUNFILES_DIR": str(root), "RUNFILES_REPO_MAPPING": str(repo_mapping)})
    assert result is not None
    return result


def test_repository_mapping_uses_explicit_source_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rf = _synthetic_runfiles(
        tmp_path,
        {
            "main_fixture/value.txt": "main repository mapping",
            "helper_fixture/value.txt": "helper repository mapping",
            "consumer_fixture/value.txt": "consumer repository mapping",
            "consumer_fixture/optional.txt": "optional consumer asset",
        },
        [",fixture,main_fixture", "helper_repo,fixture,helper_fixture", "external_consumer,fixture,consumer_fixture"],
    )
    monkeypatch.setattr(runfiles, "_get_runfiles", lambda: rf)

    assert runfiles.get_required_path("fixture/value.txt", source_repo="").read_text() == "main repository mapping"
    assert (
        runfiles.get_required_path("fixture/value.txt", source_repo="helper_repo").read_text()
        == "helper repository mapping"
    )
    assert (
        runfiles.get_required_path("fixture/value.txt", source_repo="external_consumer").read_text()
        == "consumer repository mapping"
    )
    optional_path = runfiles.find_path("fixture/optional.txt", source_repo="external_consumer")
    assert optional_path is not None
    assert optional_path.read_text() == "optional consumer asset"


def test_own_repo_rlocation_survives_a_call_from_another_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actual_runfiles = _synthetic_runfiles(
        tmp_path,
        {"ducktape+/pkg/data.txt": "owned by this repository"},
        ["external_consumer,ducktape,consumer_ducktape"],
    )

    monkeypatch.setattr(actual_runfiles, "CurrentRepository", Mock(return_value="ducktape+"))
    monkeypatch.setattr(runfiles, "_get_runfiles", Mock(return_value=actual_runfiles))

    rlocation = runfiles.own_repo_rlocation("pkg/data.txt")
    assert rlocation == "ducktape+/pkg/data.txt"
    path = runfiles.get_required_path(rlocation, source_repo="external_consumer")
    assert path.read_text() == "owned by this repository"


def test_missing_optional_file_is_none_but_required_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rf = _synthetic_runfiles(tmp_path, {}, [])
    monkeypatch.setattr(runfiles, "_get_runfiles", lambda: rf)

    assert runfiles.find_path("_main/absent.txt") is None
    with pytest.raises(RuntimeError, match="Resolved path does not exist"):
        runfiles.get_required_path("_main/absent.txt")


if __name__ == "__main__":
    pytest_bazel.main()
