"""Request/response wire types for the model-only calibration endpoints.

These mirror `augur.product.wire`: Pydantic models at the HTTP boundary, snake_case
on the wire (the frontend camelizes), exported to the frontend Zod schema via
`augur.api.export_schema`. The run response embeds the calibration library's
`CalibrationResult` (clean/surfaced rows) plus issuer fan charts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PositiveInt

from finance.augur.api.schemas import ApiModel
from finance.augur.calibration.calibration import CalibrationResult, MarkFan
from finance.augur.model.sample_sanity import SanityBandResult
from finance.augur.product.wire import MAX_HORIZON_MONTHS

# Default percentile bands for the issuer mark fan (5/25/50/75/95).
CALIBRATION_FAN_PERCENTILES: tuple[float, ...] = (5.0, 25.0, 50.0, 75.0, 95.0)


class CalibrationSanityBand(ApiModel):
    """Wire mirror of `augur.model.sample_sanity.SanityBandResult`: one evaluated
    reasonableness band (expected bounds vs the observed value(s)) for the calibration page.

    `expected_lower`/`expected_upper`/`month` are legitimately `None` for some bands (a one-sided
    bound, or a check with no month index); the drop-None wire omits them and the frontend Zod
    schema treats them as optional, so it can distinguish "no bound" from a present bound.
    """

    label: str
    series_id: str
    kind: str
    month: int | None = None
    expected_lower: float | None = None
    expected_upper: float | None = None
    observed: list[float] = Field(default_factory=list)
    observed_labels: list[str] = Field(default_factory=list)
    # `unmodeled` = this band's series/issuer isn't produced by the deployment's preset; the
    # frontend renders these distinctly from `skipped` (band's month exceeds the sampled horizon
    # but the series is otherwise emitted) and `fail` (band failed against an emitted series).
    status: Literal["pass", "fail", "skipped", "unmodeled"]
    detail: str


def sanity_band_to_wire(result: SanityBandResult) -> CalibrationSanityBand:
    """Map the pure-evaluator `SanityBandResult` dataclass onto its wire model."""
    return CalibrationSanityBand(
        label=result.label,
        series_id=result.series_id,
        kind=result.kind,
        month=result.month,
        expected_lower=result.expected_lower,
        expected_upper=result.expected_upper,
        observed=list(result.observed),
        observed_labels=list(result.observed_labels),
        status=result.status,
        detail=result.detail,
    )


class CalibrationRunRequest(ApiModel):
    preset_id: str | None = Field(
        default=None,
        description=(
            "Exogenous preset id (a key of `models`) whose model includes the catalog's "
            "issuer; defaults to the deployment's `default_model_id`."
        ),
    )
    horizon_months: PositiveInt = Field(default=120, le=MAX_HORIZON_MONTHS)
    rollouts: PositiveInt = Field(default=2000, description="Number of rollout paths (one seed each).")
    seed: int = Field(default=1701, ge=0, description="First rollout seed; seeds run [seed, seed + rollouts).")


class CalibrationRunResponse(ApiModel):
    """`CalibrationResult` (clean/surfaced/categorical rows) plus a per-issuer fan chart for each
    PE issuer the catalog scores.

    `mark_fans` / `valuation_fans` carry one `MarkFan` per emitted referenced issuer (each fan
    names its `issuer`); `valuation_fans` only includes issuers whose opt-in valuation channel is
    on. `sanity_bands` carries the deployment's `sample_sanity` reasonableness bands evaluated
    against this run's rollouts; empty when the deployment configures no `sample_sanity_path`.
    """

    preset_id: str
    result: CalibrationResult
    mark_fans: list[MarkFan] = Field(default_factory=list)
    valuation_fans: list[MarkFan] = Field(default_factory=list)
    sanity_bands: list[CalibrationSanityBand] = Field(default_factory=list)
