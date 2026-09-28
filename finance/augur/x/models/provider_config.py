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
# opportunity events. `structural_macro` (its own example is below) models public
# markets only, so it synthesizes no PE fallbacks.
type: composite
macro:
  type: structural_macro
  equity:
    instrument: {symbol: SPY, initial_price_usd: 500.0}
  instruments:
    - {symbol: BOND_FUND, maturity_years: 6.0, initial_price_usd: 100.0}
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
# scalar, crypto/home_value/rent are keyed by sub-id. No magic-prefix keys anywhere.
type: independent
asset_prices:
  security:
    SPY: {kind: gbm, initial_value: 1.0, monthly_log_return_mu: 0.00477, monthly_log_return_sigma: 0.04619}
    btc: {kind: constant, value: 75000.0}
index_series:
  inflation: {kind: gbm, initial_value: 1.0, monthly_log_return_mu: 0.00237, monthly_log_return_sigma: 0.00433}
```

```yaml
# Small structural macro model. Two latent rates drive every instrument, so a rate move
# prices the whole sleeve coherently: a fund's price falls and its payout climbs (slowly,
# over its duration) off the same state. Instruments are ROWS, not extra random walks — a
# symbol, a duration, and a spread over the curve at that duration. `macro_state` and
# equity's `mean`/`monthly_log_return_sigma` default to the checked-in fit
# (calibrated/trained_structural_macro.yaml) when omitted, as here.
type: structural_macro
equity:
  instrument: {symbol: VOO, initial_price_usd: 520.0}
  # To replace the fitted drift, pin a long-run expected annual return and cite it:
  # mean: {kind: pinned, annual_arithmetic_mean: 0.0875, citation: "AAA C-3 Phase 2 RBC report, March 2005, pp. 23-25"}
instruments:
  - {symbol: VMFXX, maturity_years: 0.0, initial_price_usd: 1.0} # cash, as an MMF holding
  - {symbol: VGIT, maturity_years: 5.3, initial_price_usd: 59.0} # intermediate Treasuries
  - {symbol: CMF, maturity_years: 5.5, initial_price_usd: 56.0, spread: -0.012} # CA munis
```

Each per-type config lives next to the model/provider it instantiates and
exposes its own `.realize_model()` method. This module is just the
discriminated union that ties them together for Pydantic's type dispatcher.
Historical replay uses date-selected materialization in experiments, not this
seeded deployment interface.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from finance.augur.model.schemas import FrozenModel
from finance.augur.x.models.composite import CompositeModel
from finance.augur.x.models.independent import IndependentProviderConfig
from finance.augur.x.models.mirroring import MirroringSampler, MirrorLevelSeries
from finance.augur.x.models.private_equity_risk import PrivateEquityRiskProviderConfig
from finance.augur.x.models.state_space import StateSpaceProviderConfig
from finance.augur.x.models.structural_macro import StructuralMacroProviderConfig
from finance.augur.x.models.trained_private_equity import TrainedPrivateEquityProviderConfig
from finance.augur.x.models.vecm import VecmProviderConfig

# The single-model providers, before the mirroring/composite wrappers that compose over them.
_LeafProviderConfig = (
    IndependentProviderConfig
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
