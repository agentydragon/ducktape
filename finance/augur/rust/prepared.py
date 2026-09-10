"""Private native/file codec for the single typed prepared-run representation."""

import json

from pydantic import TypeAdapter

from finance.augur.sim.prepared import CompiledRun

_RUN = TypeAdapter(CompiledRun)


def _encode(run: CompiledRun) -> str:
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    return _RUN.dump_json(run, by_alias=True, warnings="error").decode()


def _decode(document: str) -> CompiledRun:
    return _RUN.validate_json(document, strict=True, extra="forbid")


def _encode_native(run: CompiledRun) -> str:
    """Native accounting receives component identity, not model state or allocation policy."""
    document = _RUN.dump_python(
        run, mode="json", by_alias=True, warnings="error", exclude={"scenario": {"_target_allocation_policies"}}
    )
    document["scenario"]["tlh_portfolios"] = [
        {
            "portfolio_id": portfolio.portfolio_id,
            "owner_agent_id": portfolio.owner_agent_id,
            "account_id": portfolio.account_id,
            "asset_id": portfolio.asset_id,
        }
        for portfolio in run.scenario.tlh_portfolios
    ]
    return json.dumps(document)
