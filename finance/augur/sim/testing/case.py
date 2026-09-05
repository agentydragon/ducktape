"""One authored scenario, in the single form any engine runs.

A case is a `Scenario` and the sampled paths it runs over, and nothing else describes it:
whatever runs it runs the plan compiled from those two. So every engine assesses one tax
schedule, one bracket ladder, one standard deduction — the compiled tables — rather than
lookups that have to agree.

That is why a case is authored here and not in an engine's own input format. An engine format
that carries tax rules lets a rule reach one engine and not another, which has happened: three
divergences were traced to a fixture stating a deduction, a §1250 rate and a capital-loss cap
that the JAX engine never read, because it resolved jurisdictions from
`sim/data/jurisdictions/*.yaml` instead. Authoring at this level makes that unrepresentable —
an engine's input is derived from the case, never authored beside it.
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
from finance.augur.sim.backend import CompiledRun
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

    The compiled plan is shared: every engine either executes it or derives its own input
    from it, so none re-derives what another already resolved. It is cached because a case is
    run more than once — an acceptance suite runs each engine, and the product suites compile
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
    def compiled_run(self) -> CompiledRun:
        """This case as the production engines take it.

        Lets a suite drive `JaxEngine`/`RustEngine` rather than a harness-only path, so what
        the differential tests compare is what the product service runs.
        """

        return CompiledRun(
            scenario=self.scenario,
            plan=self.plan,
            external_series=self.external_series,
            jurisdictions=self.jurisdictions,
            locations=dict(self.locations),
        )
