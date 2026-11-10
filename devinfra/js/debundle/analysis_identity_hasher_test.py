"""Tests the cached identity hash against the pre-cache record stream."""

import hashlib
import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.js.debundle.analysis_identity_hasher import (
    AnalysisManifest,
    RootFragment,
    analysis_digest,
    build_root_fragments,
    main,
    parse_root_fragments,
)

_FILES = (
    "fixture/a_cached.txt",
    "fixture/b_uncached.txt",
    "fixture/c_cached_tree",
    "fixture/c_cached_tree/a.bin",
    "fixture/e_file_link",
    "fixture/f_uncached.txt",
    "fixture/z_cached.txt",
)
_CACHED_FILES = ("fixture/a_cached.txt", "fixture/c_cached_tree", "fixture/z_cached.txt")
_CONFIG = {"features": ["a", "b"], "mode": "opt"}
_OLD_DIGEST = "d60fe4467fa2c8956ef819f2937d1d286d112d1bc335e989a016218002b5bdd4"


def _create_inputs(root: Path) -> None:
    (root / "fixture/c_cached_tree/nested").mkdir(parents=True)
    (root / "fixture/c_cached_tree/empty").mkdir()
    (root / "fixture/a_cached.txt").write_bytes(b"cached-one\n")
    (root / "fixture/b_uncached.txt").write_bytes(b"uncached-one\n")
    (root / "fixture/c_cached_tree/a.bin").write_bytes(b"\x00\x01")
    (root / "fixture/c_cached_tree/nested/z.h").write_bytes(b"#define X 7\n")
    (root / "fixture/e_file_target.txt").write_bytes(b"symlink target\n")
    (root / "fixture/e_file_link").symlink_to("e_file_target.txt")
    (root / "fixture/f_uncached.txt").write_bytes(b"uncached-two\n")
    (root / "fixture/z_cached.txt").write_bytes(b"cached-two\n")


def _legacy_digest(config: object, files: tuple[str, ...]) -> str:
    """Independent copy of the original single-pass encoding for a test oracle."""
    digest = hashlib.sha256()

    def record(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "little"))
        digest.update(value)

    record(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
    for name in files:
        root = Path(name)
        record(name.encode())
        record(b"directory" if root.is_dir() else b"file")
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_dir():
                if path.is_symlink():
                    raise ValueError(f"unsupported directory symlink in analysis identity input: {path}")
                record(str(path).encode())
                record(b"directory")
                continue
            record(str(path).encode())
            content = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    content.update(block)
            record(content.digest())
    return digest.hexdigest()


def _run_pipeline(
    root: Path, config: object, files: tuple[str, ...] = _FILES, cached_files: tuple[str, ...] = _CACHED_FILES
) -> tuple[str, str]:
    cache_manifest = root / "cached-roots.json"
    fragments = root / "cached-records.json"
    manifest = root / "manifest.json"
    output = root / "identity.rs"
    cache_manifest.write_text(json.dumps({"files": list(cached_files)}))
    manifest.write_text(json.dumps({"config": config, "files": list(files), "cached_files": list(cached_files)}))
    main(["cache", str(cache_manifest), str(fragments)])
    main(["hash", str(manifest), str(fragments), str(output)])
    return output.read_text(), fragments.read_text()


def test_cached_and_uncached_records_preserve_the_old_digest(tmp_path: Path, monkeypatch) -> None:
    _create_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)

    expected = _legacy_digest(_CONFIG, _FILES)
    assert expected == _OLD_DIGEST

    cached_output, cached_records = _run_pipeline(tmp_path, _CONFIG)
    assert cached_output == ('pub const ANALYSIS_ENGINE_SHA256: &str = "' + _OLD_DIGEST + '";\n')

    uncached_output, _ = _run_pipeline(tmp_path, _CONFIG, cached_files=())
    assert uncached_output == cached_output

    changed_config = {"features": ["a", "b"], "mode": "debug"}
    config_output, config_records = _run_pipeline(tmp_path, changed_config)
    assert config_records == cached_records
    assert config_output != cached_output
    assert config_output.endswith(_legacy_digest(changed_config, _FILES) + '";\n')

    (tmp_path / "fixture/b_uncached.txt").write_bytes(b"uncached changed\n")
    changed_uncached, uncached_records = _run_pipeline(tmp_path, _CONFIG)
    assert uncached_records == cached_records
    assert changed_uncached.endswith(_legacy_digest(_CONFIG, _FILES) + '";\n')
    assert changed_uncached != cached_output

    (tmp_path / "fixture/a_cached.txt").write_bytes(b"cached changed\n")
    changed_cached, changed_cached_records = _run_pipeline(tmp_path, _CONFIG)
    assert changed_cached_records != cached_records
    assert changed_cached.endswith(_legacy_digest(_CONFIG, _FILES) + '";\n')
    assert changed_cached != changed_uncached


def test_cached_root_metadata_must_cover_each_requested_root(tmp_path: Path, monkeypatch) -> None:
    _create_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest = AnalysisManifest(_CONFIG, _FILES, _CACHED_FILES)
    fragments = build_root_fragments(_CACHED_FILES)

    with pytest.raises(ValueError, match="fragment count does not match"):
        analysis_digest(manifest, fragments[:-1])

    with pytest.raises(ValueError, match="fragment order does not match"):
        analysis_digest(manifest, (fragments[1], fragments[0], fragments[2]))

    duplicate = RootFragment(name=fragments[0].name, records=fragments[0].records)
    malformed = {"roots": [fragments[0].as_json(), duplicate.as_json(), fragments[2].as_json()]}
    with pytest.raises(ValueError, match="duplicate roots"):
        parse_root_fragments(malformed)


def test_nested_directory_symlinks_are_rejected_in_both_modes(tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "fixture/tree"
    target = tmp_path / "fixture/target"
    tree.mkdir(parents=True)
    target.mkdir()
    (tree / "nested-link").symlink_to(target, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="unsupported directory symlink"):
        build_root_fragments(("fixture/tree",))

    uncached_manifest = AnalysisManifest(config=_CONFIG, files=("fixture/tree",), cached_files=())
    with pytest.raises(ValueError, match="unsupported directory symlink"):
        analysis_digest(uncached_manifest, ())


if __name__ == "__main__":
    pytest_bazel.main()
