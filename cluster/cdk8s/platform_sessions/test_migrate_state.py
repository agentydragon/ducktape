"""The storage migration preserves state across failure and restart."""

from pathlib import Path

import pytest
import pytest_bazel

from cluster.cdk8s.platform_sessions import migrate_state


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    private = source / "session" / "provider"
    private.mkdir(parents=True, mode=0o700)
    (private / "state.json").write_text("original private state")
    (private / "state.json").chmod(0o600)
    target = tmp_path / "target"
    target.mkdir()
    return source, target


def test_copy_and_restart_never_restore_stale_source(tmp_path: Path) -> None:
    source, target = _source(tmp_path)
    migrate_state.migrate(source, target)
    copied = target / "state" / "session" / "provider" / "state.json"
    assert copied.read_text() == "original private state"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert (target / "state").stat().st_mode & 0o777 == 0o700
    copied.write_text("newer state")
    (source / "session" / "provider" / "state.json").write_text("stale state")
    migrate_state.migrate(source, target)
    assert copied.read_text() == "newer state"


def test_failure_does_not_publish_partial_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(tmp_path)
    real_digest = migrate_state._digest
    monkeypatch.setattr(migrate_state, "_digest", lambda _: (_ for _ in ()).throw(OSError("injected failure")))
    with pytest.raises(OSError, match="injected failure"):
        migrate_state.migrate(source, target)
    assert not (target / "state").exists()
    assert list(target.iterdir()) == []
    monkeypatch.setattr(migrate_state, "_digest", real_digest)
    migrate_state.migrate(source, target)
    assert (target / "state" / "session" / "provider" / "state.json").read_text() == "original private state"


def test_unrecognized_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    source, target = _source(tmp_path)
    state = target / "state"
    state.mkdir()
    (state / "other-data").write_text("keep")
    with pytest.raises(ValueError, match="unrecognized state"):
        migrate_state.migrate(source, target)
    assert (state / "other-data").read_text() == "keep"


def test_source_symlink_is_not_followed(tmp_path: Path) -> None:
    source, target = _source(tmp_path)
    (source / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="unsupported file type"):
        migrate_state.migrate(source, target)
    assert not (target / "state").exists()


if __name__ == "__main__":
    pytest_bazel.main()
