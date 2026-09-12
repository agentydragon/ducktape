"""Persist the single typed prepared-run representation for reproducible experiments."""

from pathlib import Path

from pydantic import TypeAdapter

from finance.augur.sim.prepared import CompiledRun

_RUN = TypeAdapter(CompiledRun)


def encode_prepared(run: CompiledRun) -> str:
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    return _RUN.dump_json(run, by_alias=True, warnings="error").decode()


def decode_prepared(document: str) -> CompiledRun:
    return _RUN.validate_json(document, strict=True, extra="forbid")


def write_prepared_input(run: CompiledRun, path: Path) -> None:
    path.write_text(encode_prepared(run))


def read_prepared_input(path: Path) -> CompiledRun:
    return decode_prepared(path.read_text())
