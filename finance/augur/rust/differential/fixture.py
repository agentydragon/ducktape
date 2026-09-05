"""A case as the Rust engine takes it.

`Case` describes a scenario and its sampled paths and nothing else, so an engine's own input
format is derived from it rather than authored beside it. This is that derivation for Rust:
it encodes the case's *compiled plan*, so the tax schedule, bracket ladder and deductions the
Rust engine assesses are the ones the JAX engine resolved, not a second lookup.

It is a function here rather than a property on `Case` because `Case` is engine-agnostic and
lives in `sim/`, which cannot import `rust/`.
"""

from __future__ import annotations

from typing import Any

from finance.augur.rust.fixture_encoder import encode_fixture
from finance.augur.sim.testing.case import Case


def fixture_for(case: Case) -> dict[str, Any]:
    return encode_fixture(
        case.scenario,
        case.plan,
        external_series=case.external_series,
        jurisdictions=case.jurisdictions,
        locations=case.locations,
    )
