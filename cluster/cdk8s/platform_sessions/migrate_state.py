"""Copy a stopped provider's SeaweedFS state to a new Piraeus volume once."""

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_MARKER = ".ducktape-seaweedfs-migration-complete"
_MARKER_CONTENT = "platform-sessions-piraeus-v1\n"


def _digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def migrate(source: Path, target: Path) -> None:
    """Publish the copy atomically; never replace an already published state."""
    state = target / "state"
    marker = state / _MARKER
    if state.exists() or state.is_symlink():
        if state.is_symlink() or not state.is_dir() or marker.is_symlink() or not marker.is_file():
            raise ValueError("Destination contains an unrecognized state directory")
        if marker.read_text() != _MARKER_CONTENT:
            raise ValueError("Destination contains an unrecognized state directory")
        _sync_directory(target)
        return

    if not source.is_dir() or (source / _MARKER).exists():
        raise ValueError("Source is missing or contains the migration marker")
    # Credentials are ordinary files and directories. Refuse unexpected entries rather
    # than follow links outside the source or silently omit an unfamiliar file type.
    for root, directories, files in os.walk(source):
        for name in directories + files:
            mode = (Path(root) / name).lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("Source contains an unsupported file type")

    staging = Path(tempfile.mkdtemp(prefix=".seaweedfs-copy-", dir=target))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        # The SeaweedFS mount root is 0777; keep the copied root private.
        staging.chmod(0o700)
        for root, _, files in os.walk(source):
            for name in files:
                old = Path(root) / name
                new = staging / old.relative_to(source)
                if _digest(old) != _digest(new) or stat.S_IMODE(old.stat().st_mode) != stat.S_IMODE(new.stat().st_mode):
                    raise ValueError("Copied state failed verification")
                with new.open("rb") as stream:
                    os.fsync(stream.fileno())
        with (staging / _MARKER).open("x") as stream:
            stream.write(_MARKER_CONTENT)
            stream.flush()
            os.fsync(stream.fileno())
        for root, _, _ in os.walk(staging, topdown=False):
            _sync_directory(Path(root))
        staging.rename(state)
        _sync_directory(target)
    finally:
        # A killed process may leave a private staging directory; it is never treated
        # as committed state. Only remove the directory created by this invocation.
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        migrate(Path("/source"), Path("/destination"))
    except Exception as exc:
        # Do not log session data, paths within it, or hashes of credentials.
        print(f"Session migration failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
