"""Persist an execution input for reproducible Python-owned experiment sessions."""

from pathlib import Path

from finance.augur.rust.prepared import _decode, _encode
from finance.augur.sim.prepared import CompiledRun


def write_prepared_input(run: CompiledRun, path: Path) -> None:
    path.write_text(_encode(run))


def read_prepared_input(path: Path) -> CompiledRun:
    """Decode once; experiment and session callers receive the same typed facts."""
    return _decode(path.read_text())
