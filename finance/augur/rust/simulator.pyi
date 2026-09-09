"""Typed surface of the `simulator` CPython extension built from `python.rs`.

The extension has no Python source for mypy to read, so this stub is the contract; it
must be edited in lockstep with the `#[pymodule]` block in `python.rs`.
"""

class ProductMetrics:
    """The seven base product metric series for one population."""

    @property
    def rollout_count(self) -> int: ...
    @property
    def snapshot_count(self) -> int: ...
    @property
    def base_series(self) -> list[list[int]]:
        """One flat row-major `[snapshot][rollout]` block per `metric_names` entry."""

    @property
    def failed_month(self) -> list[int]:
        """Per-rollout failure month; `-1` for a rollout that never failed."""

    @property
    def metric_names(self) -> list[str]: ...

def simulate_product_metrics(fixture_json: str, primary_agent_id: str) -> ProductMetrics: ...
def simulate_dense_json(fixture_json: str) -> str: ...
def simulate_forensic_json(fixture_json: str) -> str: ...
def simulate_summaries_json(fixture_json: str) -> str: ...

class SpendingObservationBatch:
    """Copied opening-month columns. Money is integer currency quanta; CPI is a ratio.

    IDs route policy memory/results to original paths; they are not policy features.
    Stopped/completed paths are absent. No future paths or mutable books cross here.
    """

    @property
    def rollout_ids(self) -> list[int]: ...
    @property
    def months(self) -> list[int]: ...
    @property
    def cash(self) -> list[int]: ...
    @property
    def public_holdings(self) -> list[int]: ...
    @property
    def price_numerators(self) -> list[int]: ...
    @property
    def price_denominators(self) -> list[int]: ...

class PrototypeSpendingSession:
    """Experimental existing-control handoff, not the supported actor-action API.

    Owns one compiled input and retained native books. Observe, decide once for each
    live month, advance. Insufficient funding stops its path; all other errors close
    this prototype session with no retry. Close explicitly if a Python policy raises.
    """

    def __init__(
        self, fixture_json: str, spending_json: str, rollout_ids: list[int], forensic: bool = False
    ) -> None: ...
    def observe(self) -> SpendingObservationBatch: ...
    def advance(self, requests: list[tuple[int, int, int]]) -> None:
        """Rows are (original path ID, observed month, requested quanta).

        Rows may be reordered/chunked. Stale, duplicate or stopped IDs are errors.
        Negative/non-integer/overflowing amounts are errors, never resubmittable.
        """

    def finish_json(self) -> str:
        """Consume terminal paths. Summary columns follow selection order.

        Compact output matches native spending Summary; forensic output is the
        canonical FramedOutput with original rollout IDs. Early finish is an error.
        """

    def close(self) -> None: ...
