# Augur plan

The conventions this plan's work follows are in `README.md` § Using Augur and `AGENTS.md`
§ Conventions. An entry leaves this file when its work lands. Current guarantees and limits
are in <SPEC.md>; capability requirements every item below must meet are in
<sim/REQUIREMENTS.md>.

## Open questions

- **The app's model selection.** The app picks its economy model through
  `x/models/provider_config.py`, a union of every provider's configuration, and reaches it and
  the fitted models through a tombstoned visibility exception. That union is the pattern
  the conventions rule out. Deciding the app's model selection is part of deciding the app's
  future, which is not decided.
- **Held feature PRs.** #8139 (uncertain equity mean), #8141 (pinned equity mean) and #8142
  (block bootstrap) would land in `x/models/`; #8143 is trading costs. Each waits on the
  owner's go.
- **Integer money.** Integer quanta were chosen for speed at large rollout counts, a gain
  never measured. If a measurement shows it does not pay, `World` may instead know its
  currency and take and return exact fixed-point `Decimal` money.

## Work

Each item is its own reviewable change; "after X" is a content prerequisite, nothing else.
Every change is checked against an independent calculation, never a copy of the engine's.

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
Francisco, mainland Vallejo and Mare Island. Today a parcel's situs is a tax rate area loaded
from `sim/data/jurisdictions/`, and `sim/property_tax.py` assesses it under Proposition 13 and
bills its secured tax in monthly twelfths in San Francisco and mainland Vallejo, and a sale pays
one flat `closing_cost_ppb`.

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
  charge from Vallejo's annual report. A hand-calculated lifecycle per location (San Francisco,
  mainland Vallejo, Mare Island) buys, holds, rents part out and sells, with every bill and
  deduction worked independently of the engine.

**Caller shape.** A purchase's `parcel` gains its districts (DISTRICTS) beside its situs and the
seller's prior assessed value.

**First milestone: San Francisco and mainland Vallejo, county and city level.** Accepted when
each of these holds against an independent calculation or published source:

- **D. Transfer tax:** San Francisco's table just below, at and above bracket edges;
  Solano County's documentary transfer tax, plus Vallejo's own if the sources show one;
  seller-paid reduces the amount realized, buyer-paid adds to basis.
- **E. Income tax:** the owner's share of ad-valorem tax paid in a calendar year goes to SALT
  under the cap, a rented share is a rental expense, transfer tax is never itemized; federal
  and California.
- **F. Lifecycle:** per location, ten years: buy with a mortgage, live there three, rent the
  whole of it four, sell. Every year's bills, SALT, rental expense, depreciation, and the
  sale's transfer tax, gain, §121 exclusion and recapture match a hand calculation checked in
  beside the test.

Outside the milestone, and listed here until done: district and parcel charges (DISTRICTS),
Proposition 8 (DECLINE), and installment timing (INSTALLMENTS); annual totals are right
without them.

**Items**, each landing its rule, data and tests together:

- **DISTRICTS**: district files with a per-category maximum, escalation and
  end date from each district's rate-and-method document: Mare Island's CFDs 2002-1, 2005-1A
  and 2005-1B (research so far: <docs/mare_island_special_taxes.md>), and San Francisco's parcel
  taxes.
- **DECLINE**: a Proposition 8 reduction while the home-value path is below the
  factored base, recovering toward it.
- **INSTALLMENTS** (after MIDYEAR): the July–June secured bill is paid in its two
  installments on their due dates.
- **DEDUCT** (after DISTRICTS): only ad-valorem tax is an itemizable real-property tax; today
  `TaxBook.property_tax` puts the whole bill, special assessment included, into SALT. On a
  rented share, service charges are expenses and local-benefit assessments are added to basis,
  federal and California.
- **TRANSFER**: at a sale the parcel's authority charges its situs's transfer
  taxes: San Francisco's tiered tax on the whole price at its bracket's rate, Solano County's
  documentary transfer tax, and Vallejo's if it has one. Seller-paid by default and reducing
  the amount realized; a buyer-paid share is added to basis. `closing_cost_ppb` splits into
  commissions, escrow/title and these taxes. Joins **Housing basis** above.
- **LIFECYCLE** (after the above): the hand-calculated lifecycle for each location.
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
- **SCORE:** compare existing fits and simple controls on the same observables, origins and
  held-out periods, with dependence-aware uncertainty or an explicit refusal to rank.
- **ROBUST** (after RUN and SCORE): select RUN's policies under each model, evaluate them on
  fresh draws under the others.
- **READY** (after ROBUST and any adopted model change): review held-out multi-horizon joint
  equity/rates/inflation behavior against agreed tolerances before claiming forecast fidelity.
  Candidates: #5487 (joint fit), #5488 (regimes), #5510 (resampling), #5835 (muni curve); the
  two-point Treasury curve beyond ten years (`model/bond_fund.py`, #5834 closed without a
  recorded reason) needs a decision whether to pursue it.

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
