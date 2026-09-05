"""What a simulation engine is, and what every engine answers with.

Two engines run Augur's scenarios: the JAX one in `sim/engine/jax_engine.py` and the Rust
one behind `rust/backend.py`. They agree on results because they run one compiled plan and
answer in the shapes declared here — `ProductMetricArrays` for the population workload,
`EventLog` for a rollout's causal trace — not because two implementations were checked
against each other.

Everything a consumer builds on top of those shapes is therefore written once. The derived
metrics, the terminal reduction and the percentile brackets are shared Python both engines
call; so is the selected-rollout projection. An engine supplies inputs, never a read model.

`sim/` cannot import `rust/`, so this contract lives here and the Rust engine implements it
from the other side.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.events import EventLog
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)
from finance.augur.sim.scenario import Scenario


class SimulationBackend(StrEnum):
    """Which engine a deployment runs.

    Named here rather than beside the engines because the deployment config has to spell it,
    and `sim/` is the one place both the config and the Rust engine can reach. Resolving a
    name to an `Engine` is `product/service.py`'s job: `sim/` cannot import `rust/`.
    """

    JAX = "jax"
    RUST = "rust"


@dataclass(frozen=True)
class CompiledRun:
    """One compiled simulation, and everything an engine needs to run it.

    `plan` is what the JAX engine executes directly. The Rust engine reads a strict integer
    fixture instead, which `rust/fixture_encoder.py` derives from this same object — so the
    two engines run one compilation of one scenario rather than two derivations of it.

    `external_series` is carried beside the plan because the compiler drops the
    private-equity company-valuation channel that no engine phase reads and the Rust
    validator still requires.
    """

    scenario: Scenario
    plan: CompiledSimulation
    external_series: ExternalSeriesContext
    jurisdictions: dict[str, Jurisdiction]
    locations: dict[str, Location]


class Engine(ABC):
    """One simulator, addressed the same way whichever it is.

    The two capture modes are separate methods because they cost different things. The
    product metrics run without retaining a monthly snapshot, journal or event trace, which
    is what makes the 100,000-rollout fan affordable; `events` retains all of it and is for
    one selected rollout.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """How this engine identifies itself in a response or a test failure."""

    @abstractmethod
    def product_metrics(self, run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
        """Every base metric series for the whole population."""

    @abstractmethod
    def product_fan(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductMetricFanSummary:
        """One metric's percentile fan, reduced without transferring every rollout."""

    @abstractmethod
    def product_terminal(self, run: CompiledRun, *, primary_agent_id: str, metric: str) -> ProductTerminalSummary:
        """One metric's terminal distribution."""

    @abstractmethod
    def product_summaries(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductProjectionSummaries:
        """The fan and the terminal distribution from one execution."""

    @abstractmethod
    def events(self, run: CompiledRun) -> EventLog:
        """The canonical event frames, in Augur's own column names and units.

        Dense: every month of every rollout is retained. Callers wanting one rollout filter
        the frames; the engines do not take a rollout index, because neither can run one
        path of a batch more cheaply than the batch.
        """
