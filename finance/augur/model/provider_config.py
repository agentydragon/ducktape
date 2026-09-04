"""Deployment's choice of exogenous model, as a discriminated YAML config.

A deployment registers one or more of these per-type configs as values in the
`Config.models` map (keyed by preset id). The augur server realizes each preset
into a runtime `Sampler` at startup and calls `.realize_model()` to build the
runtime exogenous model. Each provider owns its own state — including current
per-issuer private-equity prices — so the simulator never has to be told about
prices out of band.

Each example below is one value in the `models` map (e.g. under `current_model:`):

```yaml
# Composite provider: a macro model owns public liquid/macro series, while a
# trained private-equity component owns the complete PE protocol series for each
# issuer: `private_equity:*` prices, auxiliary liquidity/control levels, and tender
# opportunity events. VECM intentionally does not synthesize PE fallbacks.
type: composite
macro:
  type: vecm
  # Written whole by `bb run //finance/augur/fit:train -- --model vecm ...` — see
  # fit/calibrated/trained_vecm_provider.yaml for the real shape. Not hand-authored;
  # a deployment copies the block the fit target wrote.
  trained_state: {factor_names: [...], train_log_levels: [[...]], params: {...}}
  latest_observations: {sp500: 5500.0, ...}
  current_mortgage30_rate_pct: 6.5
private_equity:
  type: trained_private_equity
  trained_model_path: /etc/augur/private_equity_model.json
```

```yaml
# Generic prior-parameter PE risk provider. Useful for fixture/prod configs that
# want the PE protocol shape without a trained private-equity artifact yet. Set
# drift/vol/probabilities to zero in tests when exact constant paths matter.
type: private_equity_risk
issuers:
  private_equity_x:
    current_mark_usd: 50.0
    monthly_log_return_mu: 0.0
    monthly_log_return_sigma: 0.0
    tender_interval_months_median: 6.0
    tender_interval_log_sigma: 0.0
```

```yaml
# Independent-per-series provider. Every level series is enumerated inside its
# role group (asset_prices / property_values / index_series); singletons are
# scalar, crypto/home_value/rent are keyed by sub-id. PE issuer marks live in their own
# `private_equity_marks` map keyed by issuer id — they are not level series, so they are
# not enumerated in any role. No magic-prefix keys anywhere.
type: independent
asset_prices:
  security:
    SPY: {kind: gbm, initial_value: 1.0, monthly_log_return_mu: 0.00477, monthly_log_return_sigma: 0.04619}
    btc: {kind: constant, value: 75000.0}
index_series:
  inflation: {kind: gbm, initial_value: 1.0, monthly_log_return_mu: 0.00237, monthly_log_return_sigma: 0.00433}
private_equity_marks:
  private_equity_x: {kind: gbm, initial_value: 50.0, monthly_log_return_mu: 0.00642, monthly_log_return_sigma: 0.10103}
```

```yaml
# Small structural macro model. Two latent rates drive every instrument, so a rate move
# prices the whole sleeve coherently: a fund's price falls and its payout climbs (slowly,
# over its duration) off the same state. Instruments are ROWS, not extra random walks — a
# symbol, a duration, and a spread over the curve at that duration. `macro_state` and
# equity's `monthly_log_return_mu`/`sigma` default to the checked-in fit
# (fit/calibrated/trained_structural_macro.yaml) when omitted, as here.
type: structural_macro
equity: {symbol: VOO, initial_price_usd: 520.0}
instruments:
  - {symbol: VMFXX, duration_years: 0.0, initial_price_usd: 1.0} # cash, as an MMF holding
  - {symbol: VGIT, duration_years: 5.3, initial_price_usd: 59.0} # intermediate Treasuries
  - {symbol: CMF, duration_years: 5.5, initial_price_usd: 56.0, spread: -0.012} # CA munis
```

```yaml
# Historical replay: one rollout per starting month of the record, no parameters at all.
# A second opinion on the fitted models rather than a replacement — it has the fat tails and
# the equity/inflation coupling they lack, and ~3 independent 30-year windows where they have
# unlimited synthetic ones. Disagreement between the two is the finding.
type: historical_windows
equity: { symbol: VOO, initial_price_usd: 520.0 }
instruments:
  - { symbol: CMF, duration_years: 5.5, initial_price_usd: 56.0, spread: -0.012 }
```

Each per-type config lives next to the model/provider it instantiates and
exposes its own `.realize_model()` method. This module is just the
discriminated union that ties them together for Pydantic's type dispatcher.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from finance.augur.model.composite import CompositeModel
from finance.augur.model.historical_windows import HistoricalWindowsProviderConfig
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.mirroring import MirroringSampler, MirrorLevelSeries
from finance.augur.model.private_equity_risk import PrivateEquityRiskProviderConfig
from finance.augur.model.schemas import FrozenModel
from finance.augur.model.state_space import StateSpaceProviderConfig
from finance.augur.model.structural_macro import StructuralMacroProviderConfig
from finance.augur.model.trained_private_equity import TrainedPrivateEquityProviderConfig
from finance.augur.model.vecm import VecmProviderConfig

# The single-model providers, before the mirroring/composite wrappers that compose over them.
_LeafProviderConfig = (
    IndependentProviderConfig
    | HistoricalWindowsProviderConfig
    | VecmProviderConfig
    | StateSpaceProviderConfig
    | StructuralMacroProviderConfig
    | TrainedPrivateEquityProviderConfig
    | PrivateEquityRiskProviderConfig
)


class MirroringProviderConfig(FrozenModel):
    type: Literal["mirroring"] = "mirroring"
    model: Annotated[_LeafProviderConfig, Field(discriminator="type")]
    mirror_series: tuple[MirrorLevelSeries, ...] = Field(min_length=1)

    def realize_model(self) -> MirroringSampler:
        return MirroringSampler(inner=self.model.realize_model(), mirror_series=self.mirror_series)


BasicProviderConfig = Annotated[_LeafProviderConfig | MirroringProviderConfig, Field(discriminator="type")]


class CompositeProviderConfig(FrozenModel):
    type: Literal["composite"] = "composite"
    macro: BasicProviderConfig
    private_equity: BasicProviderConfig

    def realize_model(self) -> CompositeModel:
        return CompositeModel(macro=self.macro.realize_model(), private_equity=self.private_equity.realize_model())


ProviderConfig = Annotated[
    _LeafProviderConfig | MirroringProviderConfig | CompositeProviderConfig, Field(discriminator="type")
]
