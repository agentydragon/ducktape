"""Evidence provenance retained with fitted artifacts, separate from runtime anchors."""

from datetime import date
from typing import Literal

from pydantic import Field

from finance.augur.model.conditioning import ExogenousObservedPoint
from finance.augur.model.schemas import FrozenModel


class FactorSeriesCalibration(FrozenModel):
    monthly_log_mu: float
    monthly_log_mu_sigma: float
    monthly_log_vol_sigma: float
    observed_months: float
    observation_count: int


class EvidenceMode(FrozenModel):
    mode: Literal["fred_only_synthesized"]
    explicit: bool
    description: str


class ReturnEvidence(FrozenModel):
    """Coverage of a source's return observations, not a runtime level anchor."""

    source_id: str = Field(min_length=1)
    factor: str | None = None
    region_name: str | None = None
    region_state: str | None = None
    used_as_marginal_evidence: bool
    return_count: int = Field(ge=0)
    first_return_month: date
    last_return_month: date
    min_duration_months: float
    max_duration_months: float


class EvidenceMetadata(FrozenModel):
    """Auxiliary evidence never silently substitutes for a missing factor anchor."""

    auxiliary_observations: dict[str, ExogenousObservedPoint] = Field(default_factory=dict)
    return_sources: tuple[ReturnEvidence, ...] = ()
    series_path_prior_calibration: dict[str, FactorSeriesCalibration] = Field(default_factory=dict)
    mode: EvidenceMode | None = None
    adjusted_close_return_count: int | None = Field(default=None, ge=0)
