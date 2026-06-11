import tarfile
from pathlib import Path

import pytest_bazel

from devinfra.ci import bb_runner_probe


def test_is_bazel_server_argv() -> None:
    assert bb_runner_probe.is_bazel_server_argv(["bazel(repo-root)", "--output_base=/tmp/out"])
    assert bb_runner_probe.is_bazel_server_argv(["java", "-jar", "/tmp/install/A-server.jar"])
    assert not bb_runner_probe.is_bazel_server_argv(["bash", "-c", "echo bazel(repo-root)"])
    assert not bb_runner_probe.is_bazel_server_argv(["awk", "/[b]azel/"])


def test_parse_proc_stat_for_summary_only() -> None:
    parsed = bb_runner_probe.parse_proc_stat(
        "1234 (bazel(repo-root)) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 424242 21"
    )

    assert parsed["pid"] == 1234
    assert parsed["comm"] == "bazel(repo-root)"
    assert parsed["state"] == "S"
    assert parsed["ppid"] == 1
    assert parsed["start_ticks"] == 424242


def test_summarize_jsonl(tmp_path: Path) -> None:
    probes = tmp_path / "probes.jsonl"

    assert bb_runner_probe.summarize_jsonl(probes) == {"exists": False, "path": str(probes)}

    probes.write_text(
        '{"phase": "before-test", "timestamp": "2026-06-10T00:00:00+00:00", "bazel_servers": []}\n'
        '{"phase": "after-test", "timestamp": "2026-06-10T00:01:00+00:00", '
        '"bazel_servers": [{"pid": 123, "start_time": "t", "age_seconds": 10, "cmdline_sha256": "abc"}]}\n'
    )
    summary = bb_runner_probe.summarize_jsonl(probes)

    assert summary["exists"] is True
    assert summary["entry_count_in_sample"] == 2
    assert summary["phases_in_sample"] == ["before-test", "after-test"]
    assert summary["last_phase"] == "after-test"
    assert summary["last_bazel_servers"] == [
        {"pid": 123, "start_time": "t", "age_seconds": 10, "cmdline_sha256": "abc"}
    ]


def test_make_archive(tmp_path: Path) -> None:
    paths = bb_runner_probe.probe_paths(tmp_path / "probe")
    paths.current.mkdir(parents=True)
    paths.probes_jsonl.write_text('{"phase": "before-test"}\n')
    (paths.current / "proc" / "before-test" / "global").mkdir(parents=True)
    (paths.current / "proc" / "before-test" / "global" / "boot_id").write_text("current-boot\n")
    (paths.latest / "proc" / "after-build" / "global").mkdir(parents=True)
    (paths.latest / "proc" / "after-build" / "global" / "boot_id").write_text("previous-boot\n")
    paths.latest_jsonl.parent.mkdir(parents=True, exist_ok=True)
    paths.latest_jsonl.write_text('{"phase": "after-build"}\n')

    info = bb_runner_probe.make_archive(paths)

    assert info["path"] == str(paths.archive)
    assert info["size"] > 0
    assert len(info["sha256"]) == 64
    with tarfile.open(paths.archive) as tar:
        names = set(tar.getnames())
    assert "probes.jsonl" in names
    assert "proc/before-test/global/boot_id" in names
    assert "previous/probes.jsonl" in names
    assert "previous/proc/after-build/global/boot_id" in names


def test_capture_bytes_records_truncation_without_stat_size(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.write_bytes(b"abcdef")

    info = bb_runner_probe.capture_bytes(src, dest, max_bytes=3)

    assert info["exists"] is True
    assert info["size"] == 3
    assert info["truncated"] is True
    assert dest.read_bytes() == b"abc"


def test_extract_digest() -> None:
    assert (
        bb_runner_probe.extract_digest(
            "uploaded /compressed-blobs/zstd/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881/1"
        )
        == "/compressed-blobs/zstd/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881/1"
    )
    assert (
        bb_runner_probe.extract_digest("2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881/1\n")
        == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881/1"
    )
    assert bb_runner_probe.extract_digest("") == ""


def test_persist_latest(tmp_path: Path) -> None:
    paths = bb_runner_probe.probe_paths(tmp_path / "probe")
    paths.current.mkdir(parents=True)
    paths.probes_jsonl.write_text('{"phase": "after-build"}\n')

    bb_runner_probe.persist_latest(paths)

    assert paths.latest_jsonl.read_text() == '{"phase": "after-build"}\n'


if __name__ == "__main__":
    pytest_bazel.main()
