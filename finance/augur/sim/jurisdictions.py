"""Tax jurisdiction definitions loaded from YAML.

A jurisdiction is one taxing authority — federal U.S., California
state, etc. Each carries bracket schedules (ordinary income;
optionally a separate LTCG schedule) and a standard deduction
keyed by filing status.

The data files live in `augur/sim/data/jurisdictions/*.yaml`. The
loader resolves them relative to this module's location so Bazel's
runfiles tree (which preserves the source layout) can find them.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_DATA_DIR = Path(__file__).parent / "data" / "jurisdictions"


class TaxBracket(BaseModel):
    """One marginal-rate slice. `upper_usd` is the inclusive upper
    edge; `math.inf` in YAML (`.inf`) denotes the open-ended top
    bracket. Brackets in a schedule are walked low to high, and the
    rate applies to income in the slice
    `(previous_upper, upper_usd]`."""

    upper_usd: float
    rate: float


class Jurisdiction(BaseModel):
    """A taxing authority's complete bracket + deduction config.

    `ltcg_brackets` is optional: when absent, the engine taxes
    long-term capital gains at the ordinary-income rate
    (California-style)."""

    jurisdiction_id: str
    ordinary_income_brackets: dict[str, list[TaxBracket]]
    ltcg_brackets: dict[str, list[TaxBracket]] | None = Field(default=None)
    standard_deduction: dict[str, float]


def load_jurisdiction(jurisdiction_id: str) -> Jurisdiction:
    """Load and validate the YAML for `jurisdiction_id`. Raises
    `FileNotFoundError` if the file is missing and Pydantic's
    `ValidationError` if the schema doesn't match."""
    path = _DATA_DIR / f"{jurisdiction_id}.yaml"
    data = yaml.safe_load(path.read_text())
    return Jurisdiction.model_validate(data)
