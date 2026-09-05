"""What a simulation engine is, and what every engine answers with.

The product read models used to name the JAX engine's own output: the selected-rollout
projection took a `CompiledSimulation` and a `DenseSimulationOutput` and indexed into them.
That put an engine's internal layout in the product's read path, where a rescaled array or a
moved field surfaced as a wrong number rather than as a broken contract.

An engine answers in the shapes declared here instead — `ProductMetricArrays` for the
population workload, `EventLog` for a rollout's causal trace. Everything built on those is
written once: the derived metrics, the terminal reduction, the percentile brackets, and the
rollout projection. An engine supplies inputs, never a read model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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
    product metrics run without retaining a monthly snapshot or event trace, which is what
    makes the 100,000-rollout fan affordable; `events` retains all of it and is for one
    selected rollout.
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
        the frames; an engine does not take a rollout index, because none can run one path of
        a batch more cheaply than the batch.
        """
