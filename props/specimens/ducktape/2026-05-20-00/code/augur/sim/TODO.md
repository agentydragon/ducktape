# Augur Sim TODO

Current tracker for gaps in `augur/sim` now that it is the primary simulation
backend for the Augur frontend/API.

## Switchover Definition

The replacement is done when the production API path is:

```text
current API / catalog config
  -> typed sim scenario + market sampling request with explicit rollout seeds
  -> augur/model JointMarketModel.sample(...)
  -> SampledMarketBundle levels/events
  -> augur/sim deterministic evaluation
  -> augur/api ProjectionRun/read models
```

`augur/api` still contains compatibility request/response schemas while
`augur/model` owns market-provider schemas and provenance helpers. The deleted
`augur/core` tree is no longer the production owner of market sampling, path
evaluation, or response semantics.

## Replacement Checklist

- [ ] Broaden the explicit translator from the current browser/API payload
      into `augur.sim.scenario.Scenario`. The first slices support generic
      SP500 lots, checking cash, monthly spend, checking-floor public-stock
      sales, month-0 property purchase, mortgage origination, and
      valuation-only private-equity lots. Tax, crypto, richer property
      cashflows, private-equity liquidity, and richer policy slices still need
      native sim translation.
- [ ] Replace the temporary sim dataframe-to-legacy-table materializer with
      serialized `ProjectionRun` read models. The current sim path derives
      monthly, terminal, fan, status, and metadata tables from
      `SimulationRun` dataframes so existing frontend graphs can smoke; the
      durable API should expose compact scenario metadata plus
      distribution-first projections instead of preserving legacy field names.
- [ ] Replace core-side required-market-key discovery with full sim scenario
      introspection so model providers know which public markets, private
      equity paths, locations, currencies, and other exogenous series must
      be sampled. The initial sim backend path already unions required level
      series across translated scenarios before sampling one shared market
      bundle.
- [ ] Add the sim-side market consumption adapter: sampled levels/events should
      drive mark-to-market asset values, rent/home-value paths, private-equity
      marks, private-equity sale opportunities, mortgage/rate paths when they
      exist, and any future exogenous streams. `augur/sim` must not sample
      production market paths internally.
- [ ] Replace bespoke partner-equity contribution handling with the generic
      property-stake model once property stakes are covered by sim tests.
- [ ] Replace ad hoc catalog/default expansion in the backend compatibility
      translator with an `AugurConfig`/catalog-to-sim scenario builder that
      remains compatible with `gaffer-private` deployment YAML.
- [ ] Continue the staged rollout after the backend sim smoke: broaden the
      fixture slice and keep browser smoke coverage on the sim-only backend.

## Frontend/API Integration Blockers

- [ ] Add API serialization, compact scenario metadata, and a frontend
      adapter over `ProjectionRun`. Prefer a clean `model -> sim -> api`
      contract over matching legacy compatibility-table names.
- [ ] Expand the backend sim smoke harness beyond the current smoke slices.
      The harness proves current `ScenarioSet` requests can translate into sim,
      sample model-owned bundles, complete `augur/sim`, and return graphable
      tables without relying on core output equality.
- [ ] Preserve legacy scalar-seed behavior only at the API compatibility edge.
      The request translator or core shim should expand scalar seed + rollout
      count into explicit rollout seeds; model implementations and sim should
      never rely on an omitted/default seed.
- [ ] Define the minimal compatibility response for existing frontend routes:
      scenario metadata, distribution summaries, selected-rollout trajectory
      series, accounting/detail drilldowns, warnings, and model provenance.
      Anything else should move behind sliced read-model endpoints instead of
      being preserved as legacy top-level fields.
- [ ] Remove or hide legacy partner-equity contribution inputs from
      frontend/backend integration until a generic, tested property-stake
      model exists. Do not add a bespoke partner-contribution pathway to
      `sim`.

## API-to-Sim Translation Inventory

This inventory tracks the current `augur.api.scenario_set.ScenarioSet`
compatibility translator, not the long-term native sim request schema. Keep the
translator honest: unsupported current-API fields should fail loudly until they
are translated or removed from the frontend path.

Already covered by the sim backend smoke:

- [x] Actors become `augur.sim.scenario.Agent` rows.
- [x] Checking account balances become `InitialAccountBalance` rows.
- [x] Request-local generic SP500 positions become `InitialLot` rows for the
      first smoke slice. This is a compatibility stopgap; durable translation
      should source real public-security lots from backend config.
- [x] Flat `MonthlySpendPolicy` becomes a recurring hard obligation.
- [x] SP500-only `CheckingFloorSellPublicStockPolicy` becomes a
      `LiquidityPolicy`.
- [x] Scalar API seed plus rollout count expands into explicit rollout seeds
      for `MarketSamplingRequest`.
- [x] Required level series are inferred from translated initial lots,
      scheduled asset sales, and liquidity-policy asset chains, then unioned
      across scenarios before sampling one shared market bundle.
- [x] Request-local private-equity holdings with positive units, basis, and
      issuer routing become valuation-only `InitialLot` rows when the browser
      leaves `value_usd` unset. This keeps the browser-shaped concentrated
      holding payload working while explicit mark anchoring and sale/tender
      behavior are still deliberately unsupported.

Translator gaps to migrate next:

- [x] Add a first-class path-indexed amount shape for configured recurring sim
      cashflows. `MarketIndexedAmount` now represents "amount is `$X` at base
      month, then scales by `model_series[reset_month] / model_series[base]`"
      and supports fixed-length reset periods such as annual rent resets.
      Future translator slices should use it for outside rent, tenant rent,
      inflation-adjusted spend, and later recurring property costs instead of
      inventing special-case growth flags.
- [ ] Define the month-0 anchoring rule for every model-driven level series.
      Public securities, home value, rent, crypto, private-equity marks, and
      inflation-indexed amounts should all say whether the configured value is
      a simulation month-0 level or a fixed contract value.
- [ ] Keep legacy aggregate public-equity values only as UI/bootstrap
      compatibility. `sp500_proxy_portfolio_usd`, `wealthfront_sp500_usd`, and
      similar display totals may be computed from configured positions, but
      sim should not treat a dollar value as a lot quantity.
- [ ] Translate tax profiles: filing status, taxing jurisdictions,
      prior-year tax for estimated payments, annual ordinary income as a
      recurring taxable income source, and any standard-deduction overrides the
      sim tax layer supports.
- [ ] Translate outside rent into recurring obligations whose amount can be
      indexed to modeled rent costs. The first translator slice may keep the
      current flat amount, but durable outside-rent behavior should consume an
      `augur/model` rent-cost series for the applicable rental market. The
      config shape should be a month-0 base rent plus a rent-cost model series
      key; sim then computes monthly rent as
      `base_rent * rent_cost_series[t] / rent_cost_series[0]`. CPI or wage
      indexing can be model choices behind that series, not hard-coded sim
      behavior.
- [ ] Drive owned-property value from modeled home-value series. A selected
      property should start from its configured/list purchase value at the
      month-0 anchor, then mark to market by the applicable
      `home_value:<location>` series. Future sale proceeds should use the same
      value path unless an explicit sale contract price exists.
- [ ] Translate financing into `MortgageFinancing`: loan amount, rate, term,
      lender agent/account, payment account, and liability id. Mortgage-rate
      sampling remains a market-model/sim capability follow-up.
- [ ] Translate property tax policy from local regulation and property
      selection. Maintenance, insurance, HOA, rental income, depreciation, and
      sale costs need either native sim policies/events or explicit deferral.
- [ ] Translate rental plans: whole-property rental, room rental, rental start
      and stop windows, vacancy assumptions, management fees, and leasing fees.
      These should become property cashflow policies or recurring
      income/obligation streams over sim state, not standalone core arrays.
      Tenant rent income should use the same rent-cost indexing contract as
      outside rent: configured month-0 rent plus a property/rental-market
      rent-cost model series, with future tenant rent scaled by
      `rent_cost_series[t] / rent_cost_series[0]`.
- [ ] Keep rental lifecycle transitions out of the initial cutover. For now,
      assume the scenario's selected rental state applies for the whole horizon;
      later browser timeline work can lower rental start/stop controls into
      explicit sim events.
- [ ] Translate special assessments into scheduled obligations.
- [ ] Translate explicit portfolio trade events into scheduled asset
      purchases/sales once sim has the needed buy-side accounting.
- [ ] Translate crypto positions into sim asset lots with per-symbol market
      series IDs, basis, quantity/value handling, and liquidity-policy
      preferences.
- [ ] Finish private-equity native sim semantics: explicit mark anchoring,
      liquidity regime, tender/public-market/acquisition constraints, and
      event stream requirements.
- [ ] Translate `PrivateEquitySalePolicy` after private-equity state and tender
      events are native to sim.
- [ ] Reintroduce private-stock sale policy controls in the frontend after the
      native sim policy exists. The sim cutover intentionally hides the old
      browser tender-policy control because selecting it generated a backend
      `PrivateEquitySalePolicy` that sim rejects; bring it back only once the
      policy lowers into native private-stock/tender events instead of the old
      core-shaped policy.
- [ ] Translate `CheckingFloorSellPublicStockPolicy.sale_asset_preference`
      beyond SP500: crypto and public-market private equity should use the
      same ordered liquidity-policy surface once those assets are native.
- [ ] Translate `PartnerEquityAccrualPolicy` only after the generic property
      stake model covers partner ownership, contribution allocation, and
      balance snapshots.
- [ ] Translate explicit property lifecycle events: property purchase,
      property sale, mortgage origination, move residence, start/stop rental,
      PE IPO, and PE acquisition. Events with no native sim semantics should
      stay hard errors.
- [ ] Keep browser timeline lowering deliberately narrow for the first cutover:
      property purchase happens at month 0, owner occupancy lasts forever when
      selected, and sale may be modeled only as an end-of-horizon event if the
      response needs it. Mid-horizon purchases, moves, and rental start/stop are
      later sim-event work.
- [ ] Decide the browser/backend ownership boundary for low-level sim programs.
      Today the browser still emits some timeline-shaped event objects while
      the backend also owns catalog defaults, financing math, counterparties,
      payments, and sim lowering. Before adding richer lifecycle controls,
      choose whether the browser sends high-level user intent only and the
      backend expands it into low-level events/payments, or whether the browser
      becomes responsible for constructing an explicit sim program.
- [ ] Split economic counterparties into explicit agents/accounts. Landlord,
      tenant, lender, seller, tax authority, HOA, insurer, and other sinks
      should not all collapse into a generic `external` account once their
      cashflows matter in reports or taxes.
- [ ] Support the account types the YAML snapshot actually needs: checking,
      taxable brokerage, crypto exchange, lender/loan-side accounts, and
      bookkeeping counterparty accounts. Escrow can stay out of scope unless a
      real workflow needs it; most account types can initially be metadata plus
      routing, not bespoke settlement logic.
- [ ] Expand required-market-series introspection to cover inflation, home
      value, owned-property rent, outside-rent cost, crypto prices,
      private-equity marks, private-equity opportunity/regime events, and
      mortgage/rate paths.
- [ ] Rename the sim/model path terminology away from "market price" where the
      value is not literally a traded unit price. Prefer a generic external
      series vocabulary at the boundary: e.g. `MarketContext` →
      `ExternalSeriesContext`, `market_prices` → `series_values`,
      `price_per_unit_usd` → `value`, and `MarketIndexedAmount` →
      `SeriesIndexedAmount`. Keep consumer-specific names precise
      (`unit_value_series_id`, `index_series_id`, rate series, event series)
      instead of forcing every external path through price/market language.
- [ ] Keep backend/sim results nominal-dollar only for the cutover. Real-dollar
      or inflation-adjusted display should be a later postprocessing/read-model
      layer, not alternate simulator accounting.
- [ ] Let the frontend omit unsupported legacy metrics during the cutover. The
      sim response should expose only metrics it can derive honestly; property,
      tax, crypto, private-equity, and detail streams can be filled back in as
      their native sim frames land.
- [ ] Make YAML configuration the source of truth for initial positions and
      other deployment facts. Bootstrap/UI defaults should be derived from the
      loaded config; scenario controls may drop or hide toggles for fields that
      are not meant to be user-twiddled in the product.

Suggested migration order:

- [ ] First: backend-configured public securities with units/price/basis,
      concrete sim lots/price paths for those securities, tax profile/ordinary
      income, outside rent, and the current SP500 spend smoke response shape.
- [ ] Second: property purchase, mortgage origination, property tax, and
      browser smoke on the sim-only backend.
- [ ] Third: crypto positions and liquidity preferences.
- [ ] Fourth: private equity, tender/public/acquisition regimes, and partner
      property stakes.
- [ ] Fifth: replace the compatibility translator with a native sim request
      schema or narrow it to legacy imports only.

## Sim Capability Gaps

- [ ] Finish the real-estate lifecycle: property sale, closing costs,
      mortgage payoff, sale proceeds split, occupancy changes,
      depreciation, §121 exclusion, §1250 recapture, itemized deductions,
      SALT cap, and qualified-residence mortgage-interest deduction.
- [ ] Reintroduce annual federal + California income-tax allocation natively
      only when sim has the underlying realized-income feeds. The deleted
      `augur/core/annual_tax.py` path handled externalized tax-parameter
      validation, federal/CA standard deductions and ordinary brackets,
      federal long-term capital-gain tiers, NIIT, unrecaptured §1250 gain,
      California behavioral-health surtax, SALT cap, qualified-residence
      mortgage-interest cap, and annual allocation of tax back to monthly
      property-sale, public-security-sale, private-equity-sale, and rental
      income sources. The sim version should be jurisdiction reference data
      plus annual tax accrual/settlement over realized sim events, not a
      core-shaped array helper.
- [ ] Add a sim-native ordered policy-program surface only if richer decisions
      need it. The deleted `augur/core/policy_runtime.py` path had ordered
      per-actor policy programs, monthly-spend debit decisions,
      checking-floor public-stock sale instructions, private-equity
      sale-opportunity decisions/applications, crypto and public-security sale
      appliers, partner contribution/ownership accrual, and property
      operating-cashflow applications. Existing sim obligations and liquidity
      policies cover part of this; missing behavior should land as typed sim
      decisions/events with explicit cause IDs.
- [ ] Consider a sim-native ledger/read-model storage layer once consumers need
      double-entry projections. The deleted core accounting tables used Polars
      dimension/fact tables for chart accounts, journal-entry kinds, journal
      entries, postings, balance snapshots, rollout identity, and materialized
      Pydantic compatibility rows. Sim should keep event/state frames as the
      source of truth and derive compact `ProjectionRun` slices first; add
      double-entry tables only as a tested reporting/read-model layer.
- [ ] Consider a declarative sim posting-schema layer only if ledger
      projections need repeated double-entry templates. The deleted core
      posting schemas described opening balances, public-security/crypto/
      private-equity/property sales, tax accrual/payment, partner
      contribution and principal-credit allocation, mortgage payment, cash
      debit obligation settlements, monthly spend, and property operating
      entries.
- [ ] Mine the deleted core `PortfolioStatement` only for config-ingestion
      ideas, not as a runtime model. The current user-friendly deployment YAML
      lives in `augur/api/portfolio.py`; future sim/config work may still want
      custody/source metadata, valuation provenance, tax-lot cost basis,
      account references, public-security lots, crypto lots, private-equity
      lots, and tender-window metadata.
- [ ] Treat `augur/model/x/legacy_market_models/` as non-runtime code. Port
      only models selected by production or used as representative joint-model
      coverage; delete or keep the rest quarantined until a fresh design pass.
- [ ] Replace the VECM wrapper's ad hoc latest-observation lookup with a
      typed evidence artifact/runtime state boundary. The model should receive
      factor-keyed current levels and provenance metadata directly, not infer
      `sp500`, home-value, rent, and inflation values from source-specific
      `latest_observations` maps.
- [ ] Add variable spending/obligation amounts sourced from exogenous
      model paths.
- [ ] Exercise constrained sellability masks end to end.

## Refactor Follow-Ups

- [ ] Revisit whether policy should emit all agent actions, including
      obligation-payment transfers. Potential future shape: hard
      demands are inputs to the agent policy, the policy emits both
      liquidation orders and checking-cash payment transfers, and
      settlement only validates that every hard demand was satisfied.
      Current split is narrower: policy emits sales; settlement emits
      required payments.
- [ ] Consider whether `EventLog` should expose only catalog-keyed
      access (`log.frame(EVENT_FRAMES.transfers)`) or keep the current
      convenience properties (`log.transfers`, etc.). The catalog now
      owns schema/normalization, but the property layer still repeats
      event names for caller ergonomics.

## Explicitly Deferred

- [ ] HIFO, specific-id, and average-cost lot selection.
- [ ] Withholding, underpayment penalties, partial obligation
      payments, delinquency balances, grace periods, and failure recovery.
- [ ] NIIT and filing statuses beyond single.
- [ ] Consider globally unique account ids to remove repeated
      `agent_id` join boilerplate.
