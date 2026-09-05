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
