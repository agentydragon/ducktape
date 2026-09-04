"""One authored scenario, in the single form both engines run.

A differential case is a `Scenario` and the sampled paths it runs over. Nothing else
describes it: the JAX engine executes the plan compiled from them, and the Rust engine runs
`fixture_encoder.encode_fixture` of that very plan. So the two engines assess one tax
schedule, one bracket ladder, one standard deduction — the compiled tables — rather than two
lookups that have to agree.

That is the whole point of authoring here rather than in the integer fixture. A fixture
carries tax rules; the JAX engine never read them, resolving jurisdictions from
`sim/data/jurisdictions/*.yaml` instead, so a fixture could state a deduction, a §1250 rate
or a capital-loss cap that only one engine ever saw. Three divergences were traced to that
before the direction was reversed, and the shape makes a fourth unrepresentable.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property
from typing import Any

import numpy as np
from jaxtyping import Float64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import LevelSeriesKey
from finance.augur.rust.fixture_encoder import encode_fixture
from finance.augur.sim.compiler.plan import CompiledSimulation, compile_simulation
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario


def scenario(initial_cash: list[InitialAccountBalance], **parts: Any) -> Scenario:
    """A scenario whose agent roster is the one its accounts name.

    Every agent a case moves money between holds an account, so `Scenario.agents` is
    derivable from `initial_cash` and is derived rather than restated beside it.
    """

    return Scenario(
        agents=[Agent(agent_id=agent_id) for agent_id in sorted({balance.agent_id for balance in initial_cash})],
        initial_cash=initial_cash,
        **parts,
    )


def levels(paths: Sequence[Sequence[Decimal]]) -> Float64[np.ndarray, " rollout snapshot"]:
    """One exogenous level series, written as one path per rollout.

    A level crosses into both engines as float64 — money in scenario currency units, an index
    as its bare level — so the authored decimals are exact only up to that boundary, which is
    the same boundary the production sampler hands the compiler.
    """

    return np.asarray([[float(value) for value in path] for path in paths], dtype=np.float64)


def flat(value: Decimal, *, rollout_count: int, horizon_months: int) -> Float64[np.ndarray, " rollout snapshot"]:
    """A level every rollout holds at one value for the whole horizon."""

    return levels([[value] * (horizon_months + 1)] * rollout_count)


@dataclass(frozen=True)
class Case:
    """A scenario, its sampled paths, and the locations its properties sit in.

    The compiled plan is shared: `run_jax` executes it and `fixture` encodes it, so neither
    engine re-derives anything the other resolved. It is cached because a case is run more
    than once — `assert_backends_agree` runs both engines, and the product suites compile
    once and dispatch per agent.
    """

    scenario: Scenario
    rollout_count: int
    # Keyed by series rather than a list of pairs: a key names its own kind and sub-id, so a
    # mapping makes a duplicated series unrepresentable instead of a silently merged one.
    series: Mapping[LevelSeriesKey, Float64[np.ndarray, " rollout snapshot"]] = field(default_factory=dict)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)
    locations: Mapping[str, Location] = field(default_factory=dict)

    @cached_property
    def external_series(self) -> ExternalSeriesContext:
        return ExternalSeriesContext.from_level_blocks(
            list(self.series.items()),
            rollout_count=self.rollout_count,
            horizon_months=int(self.scenario.horizon_months),
            private_equity=self.private_equity,
        )

    @cached_property
    def jurisdictions(self) -> dict[str, Jurisdiction]:
        """The deployment's own tax law, which is what the JAX engine resolves for a scenario.

        Both engines read it through the compiled plan, so the rates, brackets, deductions and
        exemptions a case is assessed under are stated in exactly one place.
        """

        return load_jurisdictions_for(self.scenario)

    @cached_property
    def plan(self) -> CompiledSimulation:
        return compile_simulation(
            self.scenario,
            rollout_count=self.rollout_count,
            external_series=self.external_series,
            jurisdictions=self.jurisdictions,
            locations=dict(self.locations),
        )

    @cached_property
    def fixture(self) -> dict[str, Any]:
        """The integer document the Rust engine consumes, encoded from this case's own plan."""

        return encode_fixture(
            self.scenario,
            self.plan,
            external_series=self.external_series,
            jurisdictions=self.jurisdictions,
            locations=self.locations,
        )
