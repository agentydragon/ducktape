"""Request/response wire types for the exogenous-only calibration endpoints.

These mirror `augur.product.wire`: Pydantic models at the HTTP boundary, snake_case
on the wire (the frontend camelizes), exported to the frontend Zod schema via
`augur.api.export_schema`. The run response embeds the calibration library's
`CalibrationResult` (clean/surfaced rows) plus a `MarkFan` for the issuer.
"""

from __future__ import annotations

from pydantic import Field, PositiveInt

from augur.api.schemas import ApiModel
from augur.calibration.calibration import CalibrationResult, MarkFan
from augur.product.wire import MAX_HORIZON_MONTHS

# Default percentile bands for the issuer mark fan (5/25/50/75/95).
CALIBRATION_FAN_PERCENTILES: tuple[float, ...] = (5.0, 25.0, 50.0, 75.0, 95.0)


class CalibrationRunRequest(ApiModel):
    preset_id: str = Field(
        description="Exogenous preset id (a key of `exogenous_presets`) whose model includes the catalog's issuer."
    )
    horizon_months: PositiveInt = Field(default=120, le=MAX_HORIZON_MONTHS)
    rollouts: PositiveInt = Field(default=2000, description="Number of rollout paths (one seed each).")
    seed: int = Field(default=1701, ge=0, description="First rollout seed; seeds run [seed, seed + rollouts).")


class CalibrationRunResponse(ApiModel):
    """`CalibrationResult` (issuer, clean/surfaced rows) plus the issuer mark fan."""

    preset_id: str
    result: CalibrationResult
    mark_fan: MarkFan
