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
- **Interest tax character:** interest is tagged with its issuer's jurisdiction
  (`InterestIncome.issuer_jurisdiction_id`), and each jurisdiction derives exemption from the
  issuer's level and whether the issuer is itself (`Jurisdiction.taxes_interest_from`). A CA
  muni is recorded as issued by `california`, which did not issue it; exemption belongs to the
  obligation's legal regime, not its issuer. Tag interest with a tax character instead:
  `Treasury`, `Municipal(state)` or `Taxable`, the three regimes products hold today. Each
  jurisdiction's data lists the characters it exempts (federal: any `municipal`; California:
  `treasury` and `municipal: california`). `character` replaces `issuer_jurisdiction_id` on
  `InterestIncome`, bond declarations, bond observations and book rows, and fund tax shares;
  shares of one character add. The issuer-level lookup and the declared-issuer checks go.
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
