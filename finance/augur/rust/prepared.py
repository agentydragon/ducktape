"""Private native/file codec for the single typed prepared-run representation."""

from pydantic import TypeAdapter

from finance.augur.sim.prepared import CompiledRun

_RUN = TypeAdapter(CompiledRun)


def _encode(run: CompiledRun) -> str:
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    return _RUN.dump_json(run, by_alias=True, warnings="error").decode()


def _decode(document: str) -> CompiledRun:
    return _RUN.validate_json(document, strict=True, extra="forbid")
