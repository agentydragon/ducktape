from pathlib import Path

import pytest
import pytest_bazel

# gazelle:include_dep //util/testing:sharding

pytest_plugins = ["pytester"]


def _run_shard(pytester: pytest.Pytester, shard_id: int, *args: str) -> list[str]:
    pytester.makepyfile("\n".join(f"def test_{i}(): pass" for i in range(7)))
    reprec = pytester.inline_run("-p", "util.testing.sharding", f"--shard-id={shard_id}", "--num-shards=3", *args)
    passed, _, _ = reprec.listoutcomes()
    return [report.nodeid.rpartition("::")[2] for report in passed]


@pytest.mark.parametrize(
    ("shard_id", "expected"),
    [(0, ["test_0", "test_3", "test_6"]), (1, ["test_1", "test_4"]), (2, ["test_2", "test_5"])],
)
def test_items_are_dealt_to_shards_by_position(pytester: pytest.Pytester, shard_id: int, expected: list[str]) -> None:
    assert _run_shard(pytester, shard_id) == expected


@pytest.mark.parametrize(("shard_id", "expected"), [(0, ["test_1"]), (1, ["test_4"]), (2, ["test_5"])])
def test_shards_what_the_filter_left(pytester: pytest.Pytester, shard_id: int, expected: list[str]) -> None:
    # Sharding before `-k` would leave shard 0 holding test_0/3/6, none of which match.
    assert _run_shard(pytester, shard_id, "-k", "test_1 or test_4 or test_5") == expected


def test_advertises_sharding_to_bazel(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_file = tmp_path / "shard_status"
    monkeypatch.setenv("TEST_SHARD_STATUS_FILE", str(status_file))
    _run_shard(pytester, 0)
    assert status_file.exists()


if __name__ == "__main__":
    pytest_bazel.main()
