from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from augur.model.location_market_sources import LocationMarketSourcesConfig
from augur.model.schemas import StrictModel


class ZillowCityRegionConfig(StrictModel):
    region_name: str
    state: str = "CA"


class SourceDataConfig(StrictModel):
    fred_sp500_csv: str
    yahoo_spy_adjusted_json: str
    fred_cpi_us_csv: str
    fred_sf_rent_cpi_csv: str
    fred_sfxrsa_csv: str
    fred_fhfa_sf_oakland_berkeley_csv: str
    fred_mortgage30_csv: str
    zillow_city_zhvi_csv: str
    zillow_home_value_regions: dict[str, ZillowCityRegionConfig] = Field(min_length=1)
    minimum_aligned_months: int = Field(default=36, ge=1)


class MarketConfig(StrictModel):
    source_data: SourceDataConfig
    location_market_sources: LocationMarketSourcesConfig

    @model_validator(mode="after")
    def _validate_location_market_sources(self) -> MarketConfig:
        home_factors = set(self.source_data.zillow_home_value_regions)
        unknown_home = {
            location_id: factor_name
            for location_id, factor_name in self.location_market_sources.home_value.items()
            if factor_name not in home_factors
        }
        if unknown_home:
            raise ValueError(
                "location_market_sources.home_value references unknown source factors "
                f"{unknown_home}; configured zillow_home_value_regions={sorted(home_factors)}"
            )

        unknown_rent = {
            location_id: factor_name
            for location_id, factor_name in self.location_market_sources.rent.items()
            if factor_name != "rent"
        }
        if unknown_rent:
            raise ValueError(
                "location_market_sources.rent references unknown source factors "
                f"{unknown_rent}; configured rent factors=['rent']"
            )
        return self


def parse_market_config(payload: Any) -> MarketConfig:
    return MarketConfig.model_validate(payload)


def load_market_config(path: Path) -> MarketConfig:
    # `yaml.safe_load` reads both YAML and JSON (JSON is a YAML subset), so either
    # extension is supported; deployments pick whichever is more ergonomic.
    return parse_market_config(yaml.safe_load(path.read_text(encoding="utf-8")))
