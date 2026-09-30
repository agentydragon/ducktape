# Augur plan

The conventions this plan's work follows are in `README.md` § Using Augur and `AGENTS.md`
§ Conventions. An entry leaves this file when its work lands. Current guarantees and limits
are in <SPEC.md>; capability requirements every item below must meet are in
<sim/REQUIREMENTS.md>.

## Tracks

In priority order; their items are under § Work.

1. **Trust in the economy model** (§ Economy model trust): no model other than historical
   replay is vetted, and every projection inherits that.
2. **A correct answer for a liquid FIRE portfolio:** BIND and EXUS, MIDYEAR, the Distributions
   and Payments tax slices, MA3 and STUDY, then RUN and ROBUST.
3. **Backstops:** GX (moving to Europe) and housing (§ Property taxes, Housing basis, GHOUSE).
4. **Consolidation:** TLHCOHORT and the app's future (§ Open questions).
5. **Deferred:** private equity (GPE; the stake is valued at $0) and performance.

## Open questions

- **The app's model selection.** The app picks its economy model through
  `x/models/provider_config.py`, a union of every provider's configuration, and reaches it and
  the fitted models through a tombstoned visibility exception. That union is the pattern
  the conventions rule out. Deciding the app's model selection is part of deciding the app's
  future, which is not decided.
- **Held feature PRs.** #8143 (trading costs) waits on the owner's go.
- **Integer money.** Integer quanta were chosen for speed at large rollout counts, a gain
  never measured. If a measurement shows it does not pay, `World` may instead know its
  currency and take and return exact fixed-point `Decimal` money.

## Work

Each item is its own reviewable change; "after X" is a content prerequisite, nothing else.
Every change is checked against an independent calculation, never a copy of the engine's.

### Economy model trust

Goal: at least one economy model other than historical replay whose 30–60 year trajectories are
believable and whose case for being sensible holds up. At that horizon trust is a reasoned
argument, not a statistical certificate: a century of US data holds about three independent
30-year windows, and no record can tell a 1% tail from a 5% one. Sanity checks catch the absurd;
the rest is judged the way one picks a model on little data: plausible assumptions, the simplest
structure that produces the behavior a decision depends on, and numbers that look right beside
history and published results. The decisions served turn on the chance of ruin and of forced
spending cuts, not on final wealth, so what a model must make believable is sequence risk: how
deep and long drawdowns and inflation episodes run, above all early in the horizon. Today
`x/models/structural_macro.py` is Gaussian and US-only, with equity independent of inflation
and no parameter uncertainty, and historical replay has only US history.

- **CARDS:** each model in `x/models/` says in prose beside its code what it assumes, what it
  leaves out, its data window, and why those choices are sensible for a 30–60 year household
  question.
- **SANITY:** a check any caller can run on sampled trajectories that rejects absurd output: an
  economy growing 10,000× in two years, a material chance of prices rising tenfold in a year.
  Starts from `x/models/sample_sanity.py`, which only the app calls today.
- **BOOT:** vet the US stationary block bootstrap (`x/models/stationary_bootstrap.py`; #5510):
  its card argues the choices, SANITY passes, and its withdrawal and ruin numbers sit plausibly
  beside Trinity and Anarkulova–Cederburg–O'Doherty. It keeps joint crashes and stagflation,
  but recombines only US history.
- **PANEL** (after BOOT): a multi-country block bootstrap on the Jordà–Schularick–Taylor
  macrohistory panel, which partly corrects US survivorship and gives an ex-US equity leg.
- **REGIME:** regime-switching lognormal equity (RSLN2) with the macro VAR, under parameter
  uncertainty (#5487, #5488): tails beyond history. Worth its complexity only if a decision
  turns on the bootstraps' floor at history's worst month.
- **ADOPT:** a model leaves `x/` when its card's argument survives review, it passes SANITY,
  and its numbers are believable beside history and published results (the American Academy
  of Actuaries' wealth factors, Anarkulova–Cederburg–O'Doherty's developed-market withdrawal
  results). Short-horizon checks on held-out data are welcome where the data allows, not a
  gate.
- **BRACKET:** a 30–60 year answer is reported across the adopted models and named stresses
  (Japan after 1990, the US 1970s), saying whether the decision survives each; where the
  models disagree, the disagreement is the finding.
- **CURVES:** a muni yield curve (#5835), and a decision whether to pursue a Treasury curve
  beyond the two points `model/bond_fund.py` interpolates (#5834 closed without a recorded
  reason).

### Taxes (closing the gaps SPEC lists in its supported tax scope)

- **Distributions:** fund capital-gain and return-of-capital payouts are implemented, or the
  products needing them rejected.
- **Payments:** per-jurisdiction estimated-payment schedules (federal equal quarters,
  California's unequal proportions) and the true-up, with the safe-harbor rule only as it
  sets the amounts; no penalties.
- **SALT phase-out:** the SALT cap's income phase-out.
- **NIIT:** net rental income becomes net investment income (`OrdinaryIncome` merges rent with
  wages today); Form 8960 line 9 deductions.
- **TIPS check:** independently reconcile maturity-period CPI change, indexed redemption and
  final taxable accretion in the existing indexed-bond slice.
- Pin cases to the selected year's IRS inflation adjustments, Publications 550 and 505,
  Form 8960 instructions, and the California estimated-payment, Schedule CA and Form 540
  instructions.

### Property taxes (San Francisco and Vallejo)

Goal: Augur computes a homeowner's and a landlord's property-related taxes correctly in San
Francisco, mainland Vallejo and Mare Island. San Francisco and mainland Vallejo are done at county
and city level (`sim/property_tax_lifecycle_test.py` holds each one's ten-year hand calculation);
Mare Island needs its districts.

**Model.**

- **Law is one jurisdiction tree.** Property-tax law goes in the same `sim/data/jurisdictions/`
  files as income-tax law, each file naming its parent: `federal` → `california` → a county
  tax rate area → special districts. Each level holds only what it sets, tagged with its
  `law_year` or fiscal year and sourced in comments beside the values. `california` holds
  Proposition 13's rules; a rate area its voter-approved debt rate per fiscal year and the
  county's documentary transfer tax; a city its transfer tax; a district its per-parcel
  charge rules.
- **A parcel names its situs; a taxpayer its residence.** A purchase carries the parcel's rate
  area and the districts that levy on it, and inherits everything above them. Income tax keeps
  following `TaxProfile.jurisdiction_ids`. "Mare Island" is Vallejo's rate area plus three CFDs,
  not a location of its own. The `home_value:`/`rent:` series key stays a market region,
  separate from the situs.
- **One authority per concern, linked by facts.** A per-parcel `PropertyTaxAuthority` computes
  the parcel's bills from its situs's rules and charges transfer tax at a sale. The per-taxpayer
  `TaxAuthority` stays as it is. Settled property-tax payments reach it through the `TaxBook`
  classified as ad-valorem tax or per-parcel charge, and it applies SALT and the rental
  schedule to them. The CA CPI factor reuses the income-tax indexation machinery.
- **The caller supplies facts about its property only:** price, dates, improvements,
  primary-residence status, and the parcel's category under each district's rate formula.
- **Data holds only what is modeled.** A field exists only where the engine reads it and a
  test pins it, and the loader rejects unknown keys. A known tax that is not modeled yet stays
  in this plan, never in the data. This is how the old files and the app's `TaxRegime` labels
  came to promise behavior that nothing implemented.
- **Two kinds of test.** Rule tests pin each rule to a published example: a
  Proposition 13 year where CPI exceeds 2%, a transfer-tax amount at a bracket edge, a CFD
  charge from Vallejo's annual report. A hand-calculated lifecycle per location buys, holds,
  rents out and sells, with every bill and deduction worked independently of the engine.

**Caller shape.** A purchase's `parcel` gains its districts (DISTRICTS) beside its situs and the
seller's prior assessed value.

Annual totals are right without DECLINE and INSTALLMENTS; Mare Island is not without DISTRICTS.

**Items**, each landing its rule, data and tests together:

- **DISTRICTS**: district files with a per-category maximum, escalation and
  end date from each district's rate-and-method document: Mare Island's CFDs 2002-1, 2005-1A
  and 2005-1B (research so far: <docs/mare_island_special_taxes.md>), and San Francisco's parcel
  taxes. A district's charge is never an itemizable real-property tax: on a rented share a
  service charge is an expense and a local-benefit assessment is added to basis, federal and
  California. Mare Island gets the same ten-year hand-calculated lifecycle as the other two
  locations, and its cases pin to the City of Vallejo's CFD reports.
- **DEBTPATH**: a rate area's debt rate for unpublished fiscal years comes from a supplied
  exogenous series, in place of carrying the last published rate forward.
- **DECLINE**: a Proposition 8 reduction while the home-value path is below the
  factored base, recovering toward it.
- **INSTALLMENTS** (after MIDYEAR): the July–June secured bill is paid in its two
  installments on their due dates.

### Calendar

- **MIDYEAR:** a world starts at a named year and month. Tax-year boundaries, payment months
  and the year close move behind one calendar helper in place of the `month % 12` sites;
  opening year-to-date income, gains, carryover and payments enter the first tax year; the
  bundled 2024 tables move to the current tax year. Accept: January and mid-year starts
  agree on overlapping tax years, and a mid-year start neither drops nor double-counts
  earlier income.
- **DATES** (after MIDYEAR): results carry the start date once and reports derive calendar
  dates from month indices.

### Products

- **BIND:** each security and managed portfolio binds to one price series tagged total-return
  or price-return, plus an optional payout series. A payout on a total-return price, or a
  price-return price without payouts unless declared non-paying, rejects before execution.
- **BOND** (after its conventions are decided: clean/dirty price, coupon and accrual dates,
  sale/redemption order, premium/discount tax, unsupported TIPS/credit cases): mark and
  partially sell the existing nominal-bond slice, then off-par acquisition. Hold-to-maturity
  becomes a policy, not the instrument's illiquidity. Then a native-position version of
  <x/bond_policies/README.md>'s supplied-curve control.
- **EXUS** (with PANEL or a fitted ex-US series): VT's and VXUS's ex-US holdings follow an
  ex-US equity path, not US returns.
- **Trading costs:** a proportional cost per trade, readable in the trace (#5486, held #8143).
- **Fund expense ratios:** a fund's annual expense ratio accrues as a drag on its value,
  readable in the trace; model them before comparing products whose costs differ materially.

### App funding path

- **Reinvestment:** the app's household never reinvests (`reinvest=None` in
  `product/scenarios.py::_funding_household`), so the app never buys or contributes, and the
  zero-mark contribution refusal (`TlhPortfolioObservation.accepts_contributions`) is reached
  only from `policy/test_cash_band_household{,_world}.py`. Turning it on is a
  `FundingPolicy.reinvest_surplus` flag, off by default, passed through as
  `Reinvest(rebalance_tolerance_ppb=None)`, with a funding-form checkbox, a
  `SCENARIO_SET_VERSION` bump and "nothing buys" dropped from `FundingPolicy`'s docstring. It
  still lacks:
  - A purchase in the timeline: `Holdings.buy` records no acquisition, so a `Buy` moves the
    cash and holding-value series but renders nothing. Needs an acquisition record captured
    into an `EventLog` frame, a purchase event in `product/wire.py` and
    `ROLLOUT_EVENT_KIND_ORDER`, and its frontend rendering.
  - A purchase pool per sleeve: purchases land in `source_account_ids[0]`, and
    `CashBandHousehold.check` refuses a sleeve without a declared pool there, while the app
    declares pools only from lots (`product/holdings.py::holding_pools`). Declare an empty pool
    for each targeted security in that account, or choose a per-sleeve purchase account.

### Experiments (caller code in `study/` and `x/`)

- **MA3:** paired TLH/no-harvest comparison on identical supplied paths and investor flows,
  on the existing `sim/tlh.py` component; cash, gains and basis reconcile to independent
  expectations; its CLI runs in CI on generated inputs, labeled apart from calibration
  evidence.
- **STUDY:** Guyton–Klinger's remaining slices (<study/guyton_klinger/PLAN.md>), a
  paper-specific glide-path consumer (Pfau–Kitces 2014) and Vanguard-style dynamic spending
  over allocation and flexibility (extending `x/bounded_spending`), each pinning its source
  conventions before its result is labeled a reproduction.
- **RUN** (after BIND, the tax slices and MIDYEAR): public synthetic-lot household example of
  a spending-flex × allocation grid on shared paths, extending `x/joint_spending_allocation`.
  Each cell reports the chance of ruin and how deep, how long and how often spending is forced
  below plan, not final wealth.
- **ROBUST** (after RUN and ADOPT): select RUN's policies under each model, evaluate them on
  fresh draws under the others.

### Gated on an owner decision

- **GHOUSE** (purchase timing): a purchase action acquires the house and signs its mortgage
  together, and failed funding stops the path with nothing half-originated. Unblocks HOUSE
  (decisions create or change contracts), SEASONED (tracked mortgages originated before month
  zero), PROPERTY (a tracked property component supplying rented share), and retiring
  `World.declare_housing`.
- **GPE** (compulsory issuer events and tenders): which compulsory events run without a tender
  policy, and when forced proceeds become spendable. Unblocks OFFERS (`Issuer` emits
  `TenderOffer`/`ForcedRecovery`, the household answers `Accept`/`Decline`), then DRAIN
  (in-month mail queued in producer order under a per-month budget; `track()` checks every
  emitted message has an acceptor), and retiring `World.declare_tender_policy`.
- **GX** (Europe backstop): destination, residency, notice and FX/tax scope. Unblocks MOVE.

### Parked

- **Performance:** batch layout, runtime and language choices wait for a measured large-N
  workload. Then VECTOR: `World` gains a rollout axis and `ActionSession` goes.
- **TLHCOHORT:** `sim/tlh.py`'s `TlhOpeningCohort` becomes the one `TlhCohort`, and the app's
  decimal `api/portfolio.py::TlhCohort` takes a config name.

## Undecided

Kept until the owner decides whether they are wanted.

- Disposition of open PR #5859 (experiment/interface sketches).
- Renaming "exogenous".
- Retiring the smooth-dilution PE mode and the PM reifier; if deleted, whether to keep a
  revival task for comparing forecasts with prediction-market beliefs.
- Reconciling the mint-stream fitter's Poisson rate with the sampler's monthly Bernoulli
  (<docs/private_equity_model.md>).
- A constituent-level TLH model, and refitting TLH decay against real-account evidence.
- Helpers for HIFO/specific-lot selection.
- Checkpoint, fork and nested-forecast feedback for a world.
- A gated refit/publish workflow for trained artifacts.
- DATES' product wire and frontend half, which waits on the app's future.
- Location-specific rent and home-value factors.
- Further experiments: an income floor plus flexible upside (Finke–Pfau–Williams 2012) and
  age-specific budgets (Blanchett 2014).
