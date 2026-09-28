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
- **Held feature PRs.** #8139 (uncertain equity mean), #8141 (pinned equity mean) and #8142
  (block bootstrap, BOOT) would land in `x/models/`; #8143 is trading costs. Each waits on the
  owner's go.
- **Integer money.** Integer quanta were chosen for speed at large rollout counts, a gain
  never measured. If a measurement shows it does not pay, `World` may instead know its
  currency and take and return exact fixed-point `Decimal` money.

## Work

Each item is its own reviewable change; "after X" is a content prerequisite, nothing else.
Every change is checked against an independent calculation, never a copy of the engine's.

### Economy model trust

Goal: at least one economy model other than historical replay that can defensibly answer a
30–60 year FIRE question. None is calibrated in any sense today: no economy model has a PIT,
coverage or held-out test. `x/models/structural_macro.py` is Gaussian and US-only, with equity
independent of inflation and no parameter uncertainty; historical replay has about three
independent 30-year windows and only US history.

- **CARDS:** each model in `x/models/` says in prose beside its code what it assumes, what it
  leaves out, its data window and the evidence behind it.
- **SANITY:** a check any caller can run on sampled trajectories that rejects absurd output
  (an economy growing 10,000× in two years). Starts from `x/models/sample_sanity.py`, which
  only the app calls today.
- **BOOT:** vet the US stationary block bootstrap (held #8142). It keeps joint crashes and
  stagflation, but recombines only US history.
- **PANEL** (after BOOT): a multi-country block bootstrap on the Jordà–Schularick–Taylor
  macrohistory panel, which partly corrects US survivorship and gives an ex-US equity leg.
- **REGIME:** regime-switching lognormal equity (RSLN2) with the macro VAR, under parameter
  uncertainty: the first model with tails beyond history.
- **SCORE:** a model leaves `x/` only after pre-registered checks: 1–5 year PIT and coverage
  on held-out origins with dependence-aware uncertainty; 5–10 year plausibility against
  published tables (the American Academy of Actuaries' wealth factors,
  Anarkulova–Cederburg–O'Doherty's developed-market results); and at 30–60 years, results
  reported as a bracket across the vetted models and named stresses, saying whether a
  decision survives it. Existing fits and simple controls are compared on the same
  observables, origins and held-out periods, or ranking is explicitly refused.
- **READY** (after ROBUST and any adopted model change): review held-out multi-horizon joint
  equity/rates/inflation behavior against agreed tolerances before claiming forecast fidelity.
  Candidates: #5487 (joint fit), #5488 (regimes), #5510 (resampling), #5835 (muni curve); the
  two-point Treasury curve beyond ten years (`model/bond_fund.py`, #5834 closed without a
  recorded reason) needs a decision whether to pursue it.

### Taxes (closing the gaps SPEC lists in its supported tax scope)

- **Distributions:** fund capital-gain and return-of-capital payouts are implemented, or the
  products needing them rejected.
- **Payments:** per-jurisdiction estimated-payment schedules (federal equal quarters,
  California's unequal proportions) and the true-up, with the safe-harbor rule only as it
  sets the amounts; no penalties.
- **SALT phase-out:** the SALT cap's income phase-out.
- **NIIT:** net rental income becomes net investment income (`OrdinaryIncome` merges rent with
  wages today); Form 8960 line 9 deductions.
- **Housing basis:** `Properties.sell` leaves out the closing costs `sim/property.py`
  capitalized at purchase. Pin which acquisition costs are capitalized, then test purchase,
  depreciation and disposal against independently calculated basis and gains.
- **TIPS check:** independently reconcile maturity-period CPI change, indexed redemption and
  final taxable accretion in the existing indexed-bond slice.
- Pin cases to the selected year's IRS inflation adjustments, Publications 550 and 505,
  Form 8960 instructions, and the California estimated-payment, Schedule CA and Form 540
  instructions.

### Property taxes (San Francisco and Vallejo)

Goal: Augur computes a homeowner's and a landlord's property-related taxes correctly in San
Francisco, mainland Vallejo and Mare Island. Today `sim/property_tax.py` bills purchase price
× one flat rate plus one flat assessment, in monthly twelfths, fixed for the whole horizon,
and a sale pays one flat `closing_cost_ppb`. The old `augur/core` engine (deleted 2026-05-20)
had Proposition 13 growth, inflation-indexed special assessments and a local transfer tax;
none came back. Each item names its location facts and is checked against a hand calculation
per location over a purchase, hold, rent-out and sale horizon.

- **ASSESS:** assessed value is its own state. It resets to the purchase price on change of
  ownership, grows each lien date by the California CPI change capped at 2%, and grows by
  capital improvements (new construction) at their cost. The ad-valorem bill is the 1% base
  plus the location's voter-approved debt rate for that fiscal year, on assessed value less
  the homeowners' exemption for a primary residence.
- **SUPPLEMENTAL:** the purchase-year supplemental assessment and bill for the difference
  between the new and prior assessed value, prorated over the rest of the fiscal year.
- **DECLINE** (after ASSESS): a Proposition 8 decline-in-value reduction when the home-value
  path falls below the factored base, recovering toward that base as the market recovers.
- **INSTALLMENTS** (after MIDYEAR): the secured bill for a July–June fiscal year is paid in
  its two installments on their due dates, not in monthly twelfths.
- **PARCEL:** each flat per-parcel charge is its own line with its own escalation rule and
  end date: San Francisco's parcel taxes and Mare Island's three CFD special taxes (CFD
  2002-1, 2005-1A and 2005-1B) from the district's rate-and-method document, including any
  sunset within the horizon.
- **DEDUCT** (after PARCEL): SALT and the rental schedule take the right parts. Only
  ad-valorem tax is an itemizable real-property tax; flat per-parcel service charges are not
  (today `TaxAuthority` itemizes the whole bill, special assessment included). On a rented
  share, service charges are deductible expenses and local-benefit assessments are added to
  basis, for federal and California.
- **TRANSFER:** a sale pays the transfer taxes where the property is: San Francisco's tiered
  real property transfer tax on the whole price at its bracket's rate, Solano County's
  documentary transfer tax, and any Vallejo city transfer tax. Seller-paid by default and
  reducing the amount realized; a buyer-paid share is added to basis. `closing_cost_ppb`
  splits into commissions, escrow/title and these taxes. Joins **Housing basis** above.
- **LOCATION** (with the first of the above to land): the simulator's `Location` carries
  these typed rules (debt rate by fiscal year, parcel charges, transfer-tax schedule) in
  place of one rate and one assessment.
- Pin cases to the San Francisco Assessor-Recorder and Treasurer-Tax Collector (secured rate,
  transfer-tax table), the Solano County Auditor-Controller's rate book, the City of Vallejo's
  CFD reports, the California Board of Equalization's inflation factor, and IRS Publications
  530 and 527.

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
- **EXUS** (with PANEL or a fitted ex-US series): VT's and VSUX's ex-US holdings follow an
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
- **ROBUST** (after RUN and SCORE): select RUN's policies under each model, evaluate them on
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
