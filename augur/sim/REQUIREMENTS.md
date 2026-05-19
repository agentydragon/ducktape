# Augur sim — requirements

A clean-rewrite target for the Augur financial-futures simulator. The
existing engine in `augur/core/scenario_engine.py` has accreted several
overlapping representations of state and time; this document captures
what the new implementation must be able to model, framed as
natural-language scenarios free of API choices. The companion design
work happens in `augur/sim/` alongside the code as it lands; nothing
here commits to a function shape, a library, or a file layout.

## What the simulator is

A double-entry bookkeeping financial simulator over a fixed sequence
of months, run as many rollouts in parallel. Each rollout is a
self-consistent timeline driven by the same exogenous market path
(or by per-rollout sampled market paths from a shared model). Agents
own cash accounts, asset positions, and liabilities; agents take
actions; actions transfer value between agents and between accounts.
Tax law sits on top: realized income and capital gains accrue tax
liability, which becomes a real obligation the agent must settle on
real-world IRS / state schedules.

Outputs: a per-month time series of every agent's full balance sheet
in every rollout, plus an append-only ledger of every transaction
that produced those balances.

## Scope

The simulator targets **federal US tax law + California tax law**.
The only tax jurisdictions that need to ship are `federal_us` and
`california`. The only locations that need to ship are a small set
of California Bay Area places (e.g. San Francisco, San Mateo, Palo
Alto, Oakland, Sunnyvale) — they differ from each other in property-
tax rate (county assessor + Prop 13 + Mello-Roos), in city-level
real-estate transfer tax (SF in particular has a graduated transfer
tax), and in HOA / special-assessment defaults, so locations stay a
configurable thing even within California.

The architecture is described in terms of jurisdictions and
locations as configurable records (see [Locations and tax
jurisdictions](#locations-and-tax-jurisdictions)) so that adding a
new state, city, or country is a parameter-record addition with no
engine change required. That's a design-quality property, not a
roadmap commitment — no scenarios exercising non-CA jurisdictions
need to ship, and no scenario crossing state lines is in scope.

## Foundational principles

These are non-negotiable shapes. Every scenario below assumes them.

- **Forward-only time.** The simulation is a strict
  `state_{t+1} = step(state_t, market_t, decisions_t)` loop. No
  post-hoc passes that re-derive earlier months from later facts; no
  "compute state at month M from the log" reconstructions running
  alongside a maintained state; no second sweep.
- **State is carried, not rebuilt.** The working state at month T+1 is
  produced from the state at T plus this month's transactions. It is
  not re-derived from scratch from independent sources each iteration.
- **Vectorized across rollouts.** Every operation — reads, decisions,
  state updates — is a bulk operation over all rollouts at once. There
  is no Python loop over rollouts anywhere in the per-month step;
  the rollout dimension is silent in policy and transition code the
  same way it is silent in an elementwise numpy add.
- **Double-entry bookkeeping.** Every transaction balances. Money does
  not appear or disappear. A transfer debits one account and credits
  another. A sale debits an asset position and credits cash. A tax
  accrual debits a tax-expense bucket and credits a tax-payable
  liability. Total assets across the system equal total liabilities +
  equity at every month boundary.
- **Single source of truth for state-over-time.** The per-month
  state-over-time is the primary output, not a derived view. Other
  outputs (net worth charts, sale-tax breakdowns, per-year tax
  liability) are projections of either the state series or the
  transaction log.
- **One representation per concept.** Cash balance is one thing,
  stored one way. Asset units / basis is one thing. Tax owed is one
  thing. There is no "the engine has it as A, the wire has it as B,
  the summary derives it as C from the log".
- **No hardcoded asset classes; generic templates instead.** The
  engine does not mention `sp500`, `crypto`, `private_equity` by name
  in its logic. It defines a small set of **asset templates** that
  capture _behavior_ (e.g. "capital-gains-eligible holding with a
  market price path and a cost basis", "depreciable real-property with
  §1250 treatment", "loan principal with an amortization schedule
  and a counterparty"). A scenario configures any number of concrete
  asset positions, each pointing at a template and supplying the
  template's parameters (display name, market-path provider, holding-
  period rule, …). Two stocks named `"foo"` and `"bar"` and a crypto
  named `"baz"` all share the same code path through the engine —
  three positions with the same template id flow through the same
  gain calculator. Adding a new named position is configuration, not
  engine code, unless it needs a genuinely new behavior template.
- **DRY rule routing.** Every rule the law (or the user) imposes on
  N things is implemented **once**, and the N things route through
  that one implementation by declaring which behavior they participate
  in. Long-term capital gain treatment is one bracket walk that
  consumes the sum of LTCG-eligible realized gains across all assets
  marked LTCG-eligible; it is not a per-asset-class function called N
  times. The same applies to deduction caps, withholding rules,
  obligation settlement chains, depreciation schedules, accrual
  cadences. If the spec changes the SALT cap or the LTCG threshold,
  exactly one place changes.
- **Vectorized hot path.** Performance is a requirement, not an
  afterthought. The per-month step runs as polars expressions / numpy
  ufuncs / equivalent bulk-vectorized ops over the rollout dimension.
  Python-level per-rollout work in the hot loop is a bug. Where a
  rule is naturally expressed as a join+group-by+window across a
  long-form frame (e.g. "this year's LTCG bucket is the sum of
  taxable_gain across all LTCG-eligible asset_change_log rows grouped
  by (rollout, year)"), that's the implementation — not a Python loop
  over rollouts that re-derives the same thing. The choice of
  numpy / polars / pytorch / something else per piece is whatever
  best vectorizes the operation; the constraint is "no per-rollout
  Python in the hot path", not the library.

Everything below builds on these.

## Asset templates and rule routing

The engine defines a small fixed set of behavior templates. Concrete
asset positions, liabilities, and obligation streams in a scenario
each point at one template and carry the template-required
parameters. Rules (tax math, depreciation, deductibility,
settlement) are implemented once per template and consume **every**
position that points at that template.

The templates below are the target set; the exact list firms up as
implementation proceeds, but the principle is fixed: every rule
applies to a template, not to a name.

- **Capital-gains-eligible holding.** A named position composed of
  one or more **tax lots**, where each lot has its own units, cost
  basis, and acquisition date. The position carries a market
  unit-price path (per-rollout per-month from the market model) and
  a cost-basis method (FIFO / LIFO / HIFO / specific-id / average
  cost) that determines which lots a sale consumes. Sales realize
  per-lot gain (`(units_sold × price) − units_sold × per_unit_basis`);
  each consumed lot's gain is classified short-term or long-term
  based on its own acquisition date vs the sale date; per-lot gains
  flow into the LTCG or ordinary-income tax buckets accordingly.
  Buying more of the same position adds a new lot (or extends an
  existing lot, in the average-cost case). The position also carries
  a **sellability mask** parameter (per-rollout per-month boolean,
  default "always sellable"); sales fire only in months where the
  mask permits, otherwise the engine queues the policy's behavior
  per its configured fallback (wait, fail, fall through to next
  funding source). PE-style lockup-and-tender positions, IPO
  lockups, and similar liquidity constraints come out of this one
  parameter without a new template. Covers everything one would
  call a stock-like, crypto-like, ETF-like, single-issuer, or
  private-equity position. Scenarios configure as many named
  positions as needed (e.g. `"sp500_etf"`, `"individual_aapl"`,
  `"bitcoin"`, `"ethereum"`, `"alice_private_equity"`) and the
  engine treats them uniformly — the lot structure, sellability,
  and basis method all live inside the template, not as
  per-asset-class concepts.
- **Depreciable real-property holding.** Capital-gains-eligible
  plus a depreciation schedule, an §1250 recapture rule on sale,
  a §121 primary-residence exclusion eligibility tracker
  (ownership + use clocks), a SALT-cap-eligible property-tax
  stream, and a qualified-residence interest seam (when the
  property is the agent's primary residence and finances it via
  an amortizing-loan template instance). Carries an
  **occupancy mode** field (primary residence / rental / vacant)
  that determines whether depreciation accrues, whether rental
  income/expenses are collected, and whether §121 eligibility is
  accruing. Mode can change mid-simulation via scheduled events.
  References a **location** (see [Locations and tax jurisdictions](#locations-and-tax-jurisdictions))
  for property-tax rate, transfer-tax rate, and applicable tax
  jurisdictions. Multiple properties — owned by the same agent
  or different agents, in the same location or different locations
  — are multiple instances of the same template. The engine does
  not branch on count.
- **Amortizing loan.** Carries outstanding principal, an
  amortization schedule, an interest rate, two counterparties
  (borrower agent and lender agent), and an interest-deductibility
  flag (e.g. qualified-residence interest is deductible up to the
  principal cap; investment-property interest follows different
  rules; an intra-family personal loan typically isn't deductible).
  One implementation covers mortgages, intra-family loans, and any
  other inter-agent fixed-payment debt. Multiple loans in one
  scenario route through the same monthly-payment + principal-vs-
  interest split + counterparty cash-flow code.
- **Tax-liability instrument.** Federal income tax + CA income tax
  for each (rollout, agent, year) — accrued at year-end based on
  the year's ordinary income + capital gains + deductions, settled
  via the IRS quarterly-estimated + year-end schedule. One
  template, parameterized by jurisdiction and filing status; runs
  once for `federal_us` and once for `california` per (agent, year)
  under the shipping scope. The structure permits adding another
  jurisdiction by parameter record only; no other jurisdiction is
  in scope.
- **Recurring obligation.** A fixed-amount fixed-cadence cash demand
  (HOA dues, insurance premium, special assessment, outside rent,
  monthly spend allowance, recurring transfer). Settles each due
  month against the obligated agent's cash via the agent's
  configured funding chain. One template, configured N times per
  scenario.
- **Income stream.** A recurring or scheduled cash inflow with a
  tax-classification label (W-2 ordinary, K-1 ordinary, rental
  income net of expenses, etc.). The cash arrives, the label feeds
  the year's tax computation, and the same income-classification
  enum is what the tax template's rules dispatch on — no
  W-2-specific code path separate from a K-1-specific one when the
  treatment is the same.

Rules attached to templates (what runs over them, exactly once):

- **Year-tax computation** consumes the year's aggregated ordinary
  income (sum across income streams marked ordinary) + the year's
  LTCG bucket (sum across capital-gains-eligible holdings'
  long-term sales) + the year's STCG bucket (short-term sales,
  routed into ordinary) + the year's depreciation recapture (from
  §1250-marked property sales) + deduction inputs (property tax,
  qualified-residence interest, SALT cap, standard deduction). It
  is one function per jurisdiction, taking aggregated inputs;
  per-asset-class versions are forbidden.
- **Per-month tax allocation** spreads the year's tax back to the
  months that produced taxable events, proportionally to each
  event's share of the year's taxable income. One function,
  consumes the per-month realized-gain stream regardless of which
  template produced each entry.
- **Quarterly estimated tax + safe harbor** is one function consuming
  the year's running income totals and the prior-year actual tax.
  Federal and CA each have their own parameters, same code.
- **Capital-gains classification** (long-term vs short-term) is one
  function consuming holding period across every capital-gains-
  eligible row. No per-asset-class duplicate.
- **Obligation funding chain** (when an agent is short of cash to
  settle a required obligation) is one function consuming the
  agent's configured chain of capital-gains-eligible positions in
  preference order. Sells happen against the same code path
  regardless of whether the position is stock-like or crypto-like.
- **Depreciation accrual** is one function consuming every
  depreciable-property-marked row's schedule + month. The engine
  doesn't have a separate "residential" and "rental" depreciation
  implementation — it has one, parameterized.

Concretely on the existing engine's smell: the current
`scenario_engine.py` has separate `sp500_*`, `crypto_*`, and
`private_equity_*` matrices, separate sale-record lists per asset
class, separate `_record_sp500_sale_*` / `_record_crypto_sale_*` /
`_record_private_equity_sale_*` recorders, and separate
`generic_sp500_sale_tax_usd` / `crypto_sale_tax_usd` /
`private_equity_sale_tax_usd` output columns. The new engine has
**one** `asset_holding_frame` keyed by `(rollout, agent, asset_id)`
with a `template_id` column, **one** sale recorder that consumes
rows of any template, **one** taxable-gain aggregator that filters by
the template's tax-treatment column. Adding a 4th capital-gains-
eligible asset class is a config change.

## Locations and tax jurisdictions

Property carrying costs, sale-side transfer taxes, and the
applicable income-tax law all vary by where the property (or the
agent) is located. The engine handles location-specific variation
via two configurable concepts wired into the templates above:

- **Tax jurisdiction.** A parameter set capturing one body of tax
  law: filing-status-keyed brackets, deductions, NIIT thresholds,
  LTCG vs ordinary treatment of capital gains, SALT cap, qualified-
  residence interest cap, depreciation schedule (e.g. 27.5-year
  residential rental), §1250 recapture rate, §121 primary-residence
  exclusion amount, deductibility rules — whatever varies between
  jurisdictions. The shipping jurisdiction set is exactly two:
  `federal_us` and `california` (see [Scope](#scope)). The tax-
  liability-instrument template **runs once per applicable
  jurisdiction per (agent, year)**, taking the jurisdiction's
  parameter set as input — so it runs twice per (agent, year) under
  the shipping scope. The structure exists so that another
  jurisdiction is a parameter-record addition with no engine
  change; no other jurisdiction needs to ship.

- **Location.** A named place (e.g. `"san_francisco"`, `"san_mateo"`,
  `"palo_alto"`, `"oakland"`, `"sunnyvale"`) carrying:
  - the ordered list of tax jurisdictions that apply to property
    or income at that location. Under the shipping scope every
    location is in California, so every location's jurisdiction
    list is `[federal_us, california]` (possibly with a city-
    level row for local transfer tax / Mello-Roos).
  - the property tax rate (county assessor + Prop 13 base + any
    Mello-Roos or parcel-tax surcharges; one rate or a more
    elaborate formula per location).
  - the transfer-tax rate that lands on sale-side closing costs
    (SF has a graduated city-level transfer tax; San Mateo
    County's is different; Santa Clara's is different again).
  - any other location-specific parameters scenarios need (e.g.
    HOA / special-assessment defaults, rent-control rules where
    they apply).

  A scenario declares the location of each property and of each
  agent's residence. Property tax on property X uses X's location's
  rate; agent A's income tax uses A's residence-location's
  jurisdictions (`[federal_us, california]` under the shipping
  scope). **One agent owning two Bay Area properties** in different
  cities is routine: each property's carrying costs and sale-side
  transfer tax follow its own location's rates; aggregate
  deductions (e.g. SALT) sum across all the agent's properties
  subject to the federal cap. The engine does not need a
  multi-location code path — the per-property and per-agent
  location parameters route into the same per-template rule.

This DRYs out "renting properties in different places" exactly the
same way the asset-template structure DRYs out asset classes:
location-specific variation is **configuration in the scenario**;
the engine routes by `location_id` on each property / agent row
into the same template-level rules. Adding a new state is a new
TaxJurisdiction record + a new Location record (or two) referencing
it; the engine doesn't notice.

### Configuration data lives in YAML, not Python

The jurisdiction and location records — federal + California
brackets, deductions, NIIT thresholds, SALT cap, qualified-residence
interest cap, LTCG threshold tables, residential-rental depreciation
schedule, §1250 rate, §121 exclusion amount, county property-tax
rates, city transfer-tax schedules, Mello-Roos defaults — are
**configuration data, not code**. They live in checked-in YAML files
that the engine reads at startup. A new tax year's bracket update
is a YAML edit; a new Bay Area location is a YAML edit; correcting
the SF transfer-tax schedule is a YAML edit. The engine never has
literal bracket boundaries or rate values inline.

Pydantic models in code validate the YAML's shape (so a typo in a
tax-bracket boundary fails at startup rather than silently doing
something wrong), but the **values** are externalized. The same
data files feed any per-jurisdiction sanity checks, golden-test
fixtures, and external auditability — there's one canonical source
for "what the law says in 2026".

This applies to anything that is "the law says" or "this place
charges X". It does **not** apply to scenario-specific
configuration (the agents in this scenario, their initial holdings,
their policies, the market paths). Scenarios are constructed
programmatically or via their own user-facing config; jurisdiction
+ location data are repo-checked-in reference data.

Existing precedent: `augur/core/annual_tax_parameters.yaml` already
holds the legacy engine's federal + CA bracket data. The new sim
extends the pattern to locations and any other "static reference
data" that varies with real-world law and place.

## Scenarios — bottom up

Each scenario adds one new capability. The simulator must handle all
of them; the order is the order in which the implementation should
acquire support, not a runtime classification.

**Notation note on asset names.** Names like `"sp500"`, `"btc"`,
`"alice_aapl"`, `"primary_residence"`, `"rental_unit_2"` appearing
below are **scenario-level configured identifiers**, not strings the
engine recognizes. They identify positions; what the engine does
with each position is determined by the template the position points
at (see [Asset templates and rule routing](#asset-templates-and-rule-routing)).
Replacing `"sp500"` with `"global_equities"` in a scenario is a
display-name change; the engine doesn't notice. Two scenarios with
ten differently-named stock-like positions each route through the
same capital-gains-eligible-holding code path.

### Layer 1: Just transfers

#### S1.1 — Alice gives Bob five dollars, once.

Alice starts with \$10 in checking. Bob starts with \$20 in checking.
At month 0, Bob transfers \$5 to Alice. After month 0: Alice has \$15,
Bob has \$15. Total system cash is \$30 before and after.

The simulator must:

- Record the transfer as a transaction with an identifiable sender,
  recipient, amount, and month.
- Produce a per-month balance series for each agent (one month long,
  in this case): Alice \$10 → \$15, Bob \$20 → \$15.
- Preserve the conservation invariant — sum across agents is \$30 at
  every month.

#### S1.2 — Transfer that's classified as income for one party.

Same as S1.1, but the transfer carries a label that marks it as
ordinary income to Alice (and not income to Bob — for Bob it's a
gift or an expense). The simulator's per-agent income totals at the
end of the year must reflect: Alice \$5 ordinary income, Bob \$0.

This is not a tax computation yet — just the classification riding
on the transaction.

#### S1.3 — Multiple agents, multiple transactions, one month.

Three agents, two transfers in the same month. The order of
application within a single month should not change end-of-month
balances when the transactions are independent. When they aren't
independent (Alice can only send what she has), the engine resolves
the cash-availability question deterministically and reports it.

### Layer 2: Time

#### S2.1 — Multiple months, no events.

One agent, \$1000 in cash, ten months, no transactions. The per-month
balance series is \$1000 repeated ten times. The simulator carries
state forward without dropping or recomputing it.

#### S2.2 — Recurring transfer.

One agent receives \$3000 paycheck on the first of every month for a
year. End-of-year balance is starting balance + \$36000. The
transaction log has twelve rows; the balance series shows monotone
growth.

### Layer 3: Rollouts

#### S3.1 — Two rollouts, deterministic, identical.

The same scenario as S2.2 but run as two parallel rollouts with no
random inputs. Both rollouts produce identical balance series. The
output is shaped so that the rollout dimension is a first-class axis
(one row per rollout-month-agent in long form, or one slice per
rollout in any per-rollout view).

#### S3.2 — Two rollouts with different exogenous market paths.

Same scenario, but a market-driven input (e.g. interest rate on a
savings account, or stock price — whichever lands first) differs
across rollouts. The two rollouts diverge in observable ways. The
simulator does not require copying state across rollouts; it just
runs the same bulk operation over the rollout axis.

#### S3.3 — Hundreds of rollouts.

Same scenario at scale. Runtime is approximately linear in rollout
count; no per-rollout Python overhead.

### Layer 4: Assets

#### S4.1 — Agent holds stock; price doesn't move.

Alice holds 100 shares of an S&P 500 ETF, cost basis \$10000, market
value \$10000 at month 0. Price flat for 12 months. End-of-year net
worth = cash + \$10000 stock. No realized gain, no tax.

#### S4.2 — Stock price moves, no sales.

Same as S4.1 but the market path goes \$100 → \$150 over 12 months.
Unrealized gain = \$5000 at month 12. Reported separately from
realized gain. No tax.

#### S4.3 — Agent sells stock at a gain.

Same as S4.2. At month 12 Alice sells 50 shares for \$7500. Cost
basis of the sold portion is \$5000 (proportional to units sold).
Realized gain is \$2500. Alice's holding drops to 50 shares cost
basis \$5000. Alice's cash gains \$7500.

The simulator must:

- Record the sale as a transaction (debit stock units + basis,
  credit cash).
- Compute the realized gain at sale time.
- Make the gain available to the tax computation (Layer 6) without
  the policy code having to know anything about tax.

#### S4.4 — Multiple lots of the same position.

Alice buys 100 shares of `"sp500_etf"` at \$100/share in month 0
(\$10000 basis, acquired month 0). She buys 50 more shares of the
same position at \$150/share in month 6 (\$7500 basis, acquired
month 6). After month 6 she holds 150 shares in one position with
two distinct tax lots: one of 100 shares cost basis \$10000 acquired
month 0, and one of 50 shares cost basis \$7500 acquired month 6.
Total basis \$17500. The simulator must keep the per-lot identity
intact across months — these are not collapsed into an aggregate
"150 shares, \$17500 basis" representation unless the position's
configured cost-basis method is average-cost.

#### S4.5 — Sale within a single lot (FIFO default).

Same setup as S4.4. The position is configured for FIFO basis. In
month 8, with the lot-0 price at \$120 and the lot-6 price at \$120,
Alice sells 75 shares. FIFO consumes 75 of the 100 shares from the
month-0 lot: 75 shares × \$100/share basis = \$7500 basis consumed,
\$9000 proceeds, \$1500 realized gain. The month-0 lot has 25
shares left (\$2500 basis); the month-6 lot is untouched (50 shares,
\$7500 basis). Total basis remaining = \$10000.

#### S4.6 — Sale crossing two lots, mixed holding periods.

Alice buys 100 shares in month 0 at \$100/share. Buys 50 more in
month 13 at \$120/share. Price in month 14 is \$130/share. Alice
sells 120 shares in month 14 (price \$130).

FIFO consumes:

- 100 shares from the month-0 lot. Held 14 months → long-term.
  Proceeds \$13000, basis \$10000, **long-term capital gain \$3000**.
- 20 shares from the month-13 lot. Held 1 month → short-term.
  Proceeds \$2600, basis \$2400, **short-term capital gain \$200**.

The year-tax computation must route these into different buckets:
the \$3000 long-term gain feeds the LTCG bracket walk; the \$200
short-term gain feeds the ordinary-income bracket walk. The
simulator does NOT merge them into a single "\$3200 capital gain"
and apply one rate.

#### S4.7 — Lot selection at sale (specific-id / HIFO).

Same setup as S4.6 but the position is configured for HIFO
(highest-in-first-out) basis selection. The month-13 lot has higher
basis-per-unit (\$120) than month-0 (\$100), so HIFO consumes
month-13 first:

- 50 shares from month-13. Held 1 month → short-term.
  Proceeds \$6500, basis \$6000, **short-term gain \$500**.
- 70 shares from month-0. Held 14 months → long-term.
  Proceeds \$9100, basis \$7000, **long-term gain \$2100**.

Total realized gain (\$2600) differs from S4.6's FIFO total
(\$3200) because different lots are consumed. The simulator
correctly routes whichever lots the configured method selects.
The same code path covers FIFO, LIFO, HIFO, specific-identification,
and average-cost — the difference is which lots are picked, not how
the gain is computed once they're picked.

#### S4.8 — Many positions, all going through one code path.

Alice owns ten differently-named positions in a single scenario:
say `"sp500_etf"`, `"intl_etf"`, `"bond_etf"`, `"bitcoin"`,
`"ethereum"`, plus five individual stocks named `"position_001"`
through `"position_005"`. Each is configured at the scenario level
with its own market price path; eight point at the standard
capital-gains-eligible-holding template (LTCG-after-1y / STCG-
otherwise); two point at a "no-LTCG-discount" variant for an
illustrative jurisdiction where everything is ordinary income. Each
position carries its own lots (see S4.4-S4.7) under its own
configured cost-basis method. The engine does NOT have ten separate
per-position code paths — every position runs through the same
template-driven code, with the position's template-id routing it
to the right rules.

Adding an 11th position is a scenario edit, not an engine edit.

### Layer 5: Market integration

#### S5.1 — Decisions depend on observed market state.

Alice's policy: "If my SP500 position is up more than 20% from
basis, sell half." Different rollouts hit the trigger at different
months (or never). The simulator evaluates the condition over the
rollout axis and fires sales in only the rollouts where it triggers.

#### S5.2 — Decisions depend on the agent's own state.

Alice's policy: "If my checking cash drops below \$100, sell \$1000
of SP500." The condition reads Alice's current cash; the decision is
"sell \$1000 of stock, deposit proceeds into checking". When the
condition holds in some rollouts and not others, the simulator
applies the action only where the condition is true. Where Alice
holds less than \$1000 of stock, the simulator sells what it can and
records the shortfall.

#### S5.3 — Market path is shared across agents but not rollouts.

Two agents in the same scenario observe the same market path within
a single rollout. Two rollouts observe two different market paths
(sampled from the configured market model). Rollouts diverge
endogenously when agents make state-dependent decisions on top of
exogenously divergent market paths.

### Layer 6: Capital gains taxation

#### S6.1 — Realized gain in a year creates a tax liability for that year.

Alice realizes \$10000 of long-term capital gain in calendar year 2025.
At year-end of 2025 the simulator computes Alice's federal tax for
2025 — which includes the \$10000 LTCG taxed at federal LTCG rates
appropriate to her total income — and accrues the corresponding
tax obligation. The obligation has a real settlement schedule (see
S6.4 / S6.5); the simulator does not pretend the tax was paid
already.

#### S6.2 — Multiple gain sources in one year combine.

Alice realizes \$10000 from `"sp500_etf"` (long-term), \$3000 from
`"bitcoin"` (long-term), and \$5000 from `"alice_private_equity"`
(long-term). All three positions point at capital-gains-eligible-
holding templates marked LTCG-on-this-sale; the year-tax math sums
their realized gains into **one** LTCG bucket and walks the federal
LTCG bracket **once**. California treats LTCG as ordinary income —
the same summed gains feed CA's ordinary-income bracket walk, also
once. There is no per-position-class tax computation; adding a 4th
LTCG-eligible position adds a row to the bucket, not a code path.

#### S6.3 — Short-term gain is ordinary income.

If a sale is held less than a year, the realized gain is taxed at
ordinary federal + CA rates rather than LTCG. The simulator carries
the held-since date (or sufficient information to derive holding
period) on every asset lot.

#### S6.4 — Quarterly estimated tax with safe harbor.

Alice has substantial investment income. To avoid IRS underpayment
penalties, she pays quarterly estimated tax on the IRS schedule:
Q1 = April 15, Q2 = June 15, Q3 = September 15, Q4 = January 15 of
the following year. Each quarterly amount is sized to satisfy the
safe-harbor rule: total quarterly payments cover the lesser of
(actual current-year tax) or (a fraction of prior-year tax —
typically 100% but 110% above the high-income threshold). For year
zero of the simulation, the prior-year tax value comes from the
scenario's tax-profile configuration (see [Resolved decisions](#resolved-decisions));
the engine does not synthesize one.

The simulator must:

- Emit quarterly obligations at the right months with the right
  amounts.
- Settle each obligation from Alice's cash, drawing on her funding
  policies (e.g. sell stock to cover) if cash is short.
- True up at year-end: the year-end obligation is `actual_year_tax
  − sum_of_quarterlies`, never producing a payment greater than the
  actual year tax across all five settlement events.

#### S6.5 — Net investment income tax.

Above the federal MAGI threshold (today \$200k single / \$200k head-
of-household), an additional 3.8% NIIT applies to investment income.
The simulator applies it correctly — including the cap at
`min(NII, MAGI − threshold)` — and folds it into the year's federal
tax. Thresholds are filing-status-keyed; MFJ thresholds aren't
relevant because MFJ is out of scope.

### Layer 7: Ordinary income taxation, US + CA

#### S7.1 — W-2 income, federal brackets, standard deduction.

Alice earns \$120000 of W-2 income for the year (the simulator
either receives this as twelve \$10000 transfers labeled income, or
as a configured annual amount; the requirement is that ordinary
income aggregates correctly). At year-end, federal tax on
`(ordinary_income − standard_deduction)` is computed via the
applicable filing-status brackets. Federal tax owed equals the
bracket walk, less any withholding the scenario configures.

#### S7.2 — California state income tax.

California treats W-2 income as ordinary, with CA's own brackets and
CA's own standard deduction. Capital gains (long-term and short-term)
are ordinary income in California — there is no preferential rate.
The simulator computes CA tax in parallel with federal, with no
shared bracket walk.

#### S7.3 — Itemized vs standard deduction.

Property tax paid + mortgage interest paid + state-income-tax paid
(subject to the federal SALT cap) is itemizable on federal. The
simulator uses the larger of itemized or standard. CA itemization
differs slightly (no SALT cap intra-state, mortgage interest treated
slightly differently); the simulator handles each jurisdiction's
rules separately.

#### S7.4 — Filing status.

Single or head-of-household. Each filing status pins different
bracket boundaries, standard deduction amounts, and threshold
values (NIIT, safe-harbor high-income). The simulator carries the
filing status per tax-paying agent and looks up the right tables.

(Married-filing-jointly and married-filing-separately are
out of scope — see [Non-goals](#non-goals). A scenario representing
a couple files each agent as single.)

#### S7.5 — Combined ordinary + capital scenario.

Alice has \$120k W-2 + \$10k LTCG + \$1500 short-term gain.
Federal tax is computed by stacking LTCG on top of ordinary income
in the LTCG bracket walk; short-term is ordinary; the simulator
produces the correct federal + CA total. The same year's safe-harbor
quarterly payments cover the right amount; the year-end true-up
clears.

### Layer 8: Mortgage — inter-agent loan with a contract

#### S8.1 — Origination.

Alice buys a house for \$500k. A lender (modeled as another agent
in the simulation — "Bank Bob") originates a 30-year fixed mortgage
at 6%, lending Alice \$400k for \$400k cash at closing. Alice
contributes \$100k from her own cash. Bank Bob debits its loan
receivable asset for \$400k and credits its cash by \$400k. Alice
debits her property asset for \$500k, debits her mortgage liability
for \$400k, credits her cash by \$100k.

The simulator models the mortgage as a **contract between two
agents** with the standard amortization schedule attached, not as
an opaque "mortgage" piece of engine state.

#### S8.2 — Monthly payment amortization.

Every month, Alice owes a payment equal to the scheduled
fixed-payment amount. The payment splits between interest (paid to
Bank Bob; reduces neither asset nor liability — flows to Bank Bob's
interest-income bucket) and principal (reduces Alice's outstanding
mortgage liability AND Bank Bob's loan receivable by the same
amount). The transaction log records both halves. Alice's checking
cash drops by the full payment. Bank Bob's cash rises by the full
payment.

#### S8.3 — Bank Bob's interest income is recorded but not taxed.

Bank Bob earns interest income on each month's interest portion of
Alice's payment. The transaction lands on Bank Bob's books — the
cash inflow + the income classification are recorded for symmetry
and for auditability — but Bank Bob is marked as a non-tax-paying
agent (see [Resolved decisions](#resolved-decisions)), so no
year-tax accrual fires on Bank Bob and Bank Bob has no tax-payable
liability. For the simulation's purposes Bank Bob is a money sink:
its cash balance is allowed to grow without bound and never matters
to any decision. Mortgage origination treats Bank Bob's cash
availability as unconditional.

#### S8.4 — Mortgage interest is deductible for Alice.

Alice's federal itemized deduction includes the year's mortgage
interest paid (subject to the federal cap on interest deductibility
above the principal cap). California also allows the deduction with
similar rules. Itemized deduction is the larger-of with standard.

#### S8.5 — Alice misses a payment.

Alice's checking cash is below the required mortgage payment at
month M. Her funding policies (sell stock first, sell crypto next,
etc., per her configured chain) attempt to cover the shortfall.
If still short, the obligation is recorded as unpaid; the
rollout's failure state is recorded; subsequent month projections
continue but the rollout is flagged. The simulator does not
crash, does not silently underpay, does not double-pay.

#### S8.6 — Mortgage payoff at property sale.

Alice sells the house for \$700k. Outstanding mortgage principal
(say \$380k) is paid off at closing — debits Alice's mortgage
liability to zero, credits Bank Bob's loan receivable to zero,
credits Bank Bob's cash by \$380k. Closing costs come out of
Alice's proceeds. Net proceeds settle to Alice's checking. The
capital gain (proceeds − adjusted basis − closing costs −
depreciation-recapture treatment) feeds year tax per Layer 6.

### Layer 9: Agent policies

#### S9.1 — Floor-triggered stock sale.

"If checking cash drops below \$100, sell \$1000 of SP500." The
policy reads the agent's cash at the start of the month, evaluates
the condition over the rollout dimension, and emits a sale decision
in rollouts where the condition holds. The decision becomes a
transaction; the transaction updates state. In rollouts where the
agent has less than \$1000 of stock available, the simulator sells
what it can and records the shortfall on the decision row.

#### S9.2 — Asset preference chain.

"If short of cash, sell SP500 first; if SP500 exhausted, sell BTC;
if both exhausted, sell private equity." The chain is per-agent
configured order; the simulator walks the chain per-rollout per-month,
applying as much as needed in priority order until the obligation
is funded or the assets are exhausted.

#### S9.3 — Reinvest excess cash.

"If checking exceeds \$50k, buy SP500 with the excess." The
condition evaluates after this month's other transactions land
(or in a configured ordering position within the month — the
simulator's per-month order is documented and stable).

#### S9.4 — Mortgage payment is non-discretionary.

The mortgage payment scenario (Layer 8) is not a policy in this
sense — it's a contract-imposed obligation that fires unconditionally
each month, settled before discretionary policies. The simulator
distinguishes obligations (required) from policies (discretionary).

#### S9.5 — Combination: monthly spend + emergency sale.

"\$3500/month spend on living expenses (debit checking). If checking
drops below \$5000 after spending, sell \$10000 of SP500." The spend
fires every month; the sale fires only in rollouts where the
post-spend cash is below the floor. Different rollouts (different
market paths affecting stock value) make different decisions.

#### S9.6 — Fixed monthly spend.

The standalone case: Alice spends \$3500 every month on living
expenses. One recurring-obligation instance with `cadence=monthly`
and a fixed amount, classified as `spending` (not income; not
deductible; non-tax-paying expense). Each month: cash drops by
\$3500; if insufficient cash, the funding chain may sell assets to
cover (same chain as any other obligation); if still short, the
month's spend obligation is unfundable and a failure-event row is
emitted.

#### S9.7 — Variable monthly spend from the market model.

Same template as S9.6, but the monthly amount is supplied by a
market-model "spending-variance" path: per-rollout per-month
amounts that differ across rollouts. The recurring-obligation
template's amount source is either a scenario-configured fixed
value (S9.6) or a market-model path (this scenario) — the engine
doesn't branch on the source; it just reads `amount_due[rollout,
month]` and settles. Different rollouts diverge endogenously
because higher-spend months drain cash faster and trigger
floor-policy sales earlier.

This is the seam that lets a scenario model "Alice's spending is
volatile" without an engine change. The same seam supports
market-driven variable rental income (S13.2) and any other
recurring obligation whose amount is rollout-dependent.

### Layer 10: Market model integration

#### S10.1 — Per-rollout sampled market paths.

The simulator integrates with an external market model that, given a
rollout count and a horizon, produces per-rollout per-month
multipliers for each asset class. Rollouts have different SP500
paths, different BTC paths, different property-value paths, etc.

#### S10.2 — Agents' decisions feed back into the rollout's trajectory.

Within a single rollout, an agent's state-dependent decision
(e.g. "sell \$10k of stock when cash low") affects that rollout's
subsequent state, but does NOT affect the market path in that
rollout — the market path is exogenous. Two rollouts with identical
market paths but different agent policies would diverge; two
rollouts with the same policies but different market paths also
diverge. The cross-product is the standard Monte Carlo behavior.

#### S10.3 — Same market model across agents.

All agents in a scenario observe the same per-rollout market path
within a rollout (one rollout has one SP500 path; both Alice and
Bank Bob see the same stock prices). The simulator does not give
different agents different views of the same asset's price.

### Layer 11: Failure modes

#### S11.1 — Cash goes negative.

If a required obligation cannot be funded even after walking the
agent's full asset chain, the simulator records the rollout as
having transitioned into a failure state at that month. State
arrays for subsequent months continue to compute (the property
keeps depreciating, the stock price keeps moving) but the rollout
carries a failure flag. The transaction log records the unpaid
obligation.

#### S11.2 — Rollout failure is per-rollout, not scenario-wide.

In a 100-rollout scenario where 12 rollouts fail at various months
and 88 stay solvent, the simulator continues all 100 rollouts and
labels the failed ones. Reported metrics distinguish "across all
rollouts" from "across surviving rollouts" where the distinction
matters.

#### S11.3 — Recovery from temporary shortfall.

If a funding policy sells assets in month M to cover a shortfall and
succeeds, the rollout does not enter failure — it continues
normally. Failure is "obligation went unpaid"; sale-driven
recovery is not failure.

### Layer 12: Housing — primary residence

#### S12.1 — Buy a house with a mortgage.

Alice buys a house in `"san_francisco"` at month 0. Purchase
price \$500k. Buy-side closing costs \$5k (configurable per-location
percentage or absolute amount). Down payment \$100k from Alice's
cash. Bank Bob originates a 30-year fixed mortgage at 6% for the
remaining \$400k (the same amortizing-loan-template instance from
S8.1).

Per S8.1's bookkeeping: Alice's checking debits \$105k (down + buy
closing). Closing costs roll into the property's **adjusted basis**
(not deductible) → the depreciable-real-property-holding instance
records `adjusted_basis = $505k`. Mortgage originates simultaneously.
The location is `"san_francisco"`; the property tax rate, transfer
tax (sell side), and applicable tax jurisdictions are pulled from
that location's config.

#### S12.2 — Monthly carrying costs while owner-occupied.

Each month while Alice lives in the house: property tax accrues
(location's rate × current property value, billed monthly or annually
per the location's cadence), HOA dues, insurance premium,
maintenance. Each is a recurring-obligation instance settling from
Alice's cash. Property-tax accruals are flagged SALT-cap-eligible
for federal itemized deduction at year-end.

#### S12.3 — Property tax + SALT cap deduction.

In a tax year where Alice pays \$12k of property tax, the federal
itemized deduction picks up \$10k (SALT cap) for federal purposes,
\$12k for CA. Combined with mortgage interest and any state-income-
tax paid, the year's itemized-deduction total is compared against
the standard deduction; the larger wins.

#### S12.4 — Qualified-residence mortgage interest deduction.

Mortgage interest paid in a year is deductible — federally up to the
interest on the first \$750k of principal (post-TCJA), CA with its
own equivalent cap. Above the cap, the deduction is proportionally
reduced (`deductible_interest = total_interest × min(1, $750k /
average_principal_balance_during_year)`). One function consumes the
amortizing-loan-template instance's principal-balance schedule and
the year's interest-paid total to produce the deductible portion.

#### S12.5 — Sell primary residence, §121 exclusion fully covers gain.

Alice sells the house at month 60 (5 years of ownership + primary-
residence use). Sale price \$700k. Sell-side closing costs \$35k
(transfer tax + agent commissions, per the location). Adjusted
basis \$505k. Realized gain = \$700k − \$35k − \$505k = \$160k.

§121 exclusion (single filer, ≥2 of last 5 years as primary residence)
allows excluding the first \$250k of gain from federal LTCG. The
\$160k gain is fully excluded; no federal LTCG accrues. California
mirrors §121. Mortgage payoff at closing (per S8.6) settles the
remaining principal back to Bank Bob.

#### S12.6 — Sell primary residence with gain above §121 cap.

Same setup as S12.5 but sale price is \$900k. Gain = \$900k − \$35k
− \$505k = \$360k. §121 excludes \$250k; the remaining \$110k is
taxable as LTCG (federal + CA), routed through the year-tax
computation alongside any other LTCG bucket entries.

#### S12.7 — Special assessment.

The HOA issues a \$20k special assessment at month 36. Recurring-
obligation instance with cadence "one-off at month 36". Settles via
Alice's configured funding chain the same way any obligation does.

### Layer 13: Housing — rental, occupancy modes, multiple properties

#### S13.1 — Convert primary residence to rental.

At month 24 Alice moves out and starts renting the house to a
tenant. The depreciable-real-property-holding instance's
`occupancy_mode` flips from `primary_residence` to `rental`.
Effects, all driven by the mode flip:

- Monthly depreciation starts accruing on the building portion of
  adjusted basis (residential rental: 27.5-year straight-line; the
  applicable jurisdiction's schedule).
- A rental-income stream and a rental-expense pipeline begin
  (mgmt fee, leasing fee, …).
- The §121 ownership clock keeps running but the use clock stops —
  later sale eligibility depends on how recent the primary-residence
  usage was.

#### S13.2 — Rental income, expenses, depreciation, net rental income.

While the house is rented (months 24-83 in our running example):

- Rental gross income (e.g. \$3000/month, possibly market-path-
  driven so different rollouts see different rents) lands in Alice's
  cash each month.
- Rental expenses (mgmt fee = 8% of gross rent; one-month leasing
  fee on the first month; the same property-tax / HOA / insurance /
  maintenance recurring obligations as S12.2) reduce taxable rental
  income.
- Accrued depreciation (computed by the depreciable-real-property
  template) reduces taxable rental income further, separately from
  cash (depreciation does not move cash; it's a tax artifact).

Net rental income for the year = `Σ(gross_income) − Σ(rental_expenses)
− Σ(depreciation)`. Folds into the year's ordinary-income bucket via
the tax-liability-instrument template. Passive-activity loss
limitations apply when net rental income is negative (modeled per
the applicable jurisdiction's rules).

#### S13.3 — Sell a rental property: §1250 depreciation recapture.

Alice sells the rental at month 84 for \$900k. Adjusted basis
\$505k. Cumulative depreciation over months 24-83 = \$50k. Sell-side
closing \$35k. Realized gain = \$900k − \$35k − \$505k = \$360k.

§1250 recapture: the portion of the gain attributable to prior
depreciation is taxed at the federal §1250 rate (capped 25%) rather
than the LTCG rate. CA: ordinary income. Recapture amount = `min(
realized_gain, cumulative_depreciation)` = \$50k. The remaining
\$310k is LTCG. §121 may not apply (or only partially apply) because
the recent primary-residence usage test fails for this timeline —
the simulator computes §121 eligibility from the ownership / use
clocks and routes the rest of the gain accordingly.

The year-tax computation must:

- Walk one federal §1250 bracket once on the recapture portion across
  all the agent's §1250-eligible property sales this year.
- Walk one federal LTCG bracket once on the LTCG portion across all
  LTCG-eligible asset sales this year (Layer 6 rules).
- Walk one CA ordinary bracket once on the combined recapture + LTCG
  amount, plus any non-rental ordinary income.

#### S13.4 — Outside rent: agent is the tenant.

Alice owns a house she rents out (S13.1-S13.3 timeline) and
simultaneously rents a place to live in. Monthly outside-rent is a
recurring-obligation instance on Alice as tenant — settles from
Alice's cash via the same funding chain as any other obligation. Not
deductible for federal personal income tax (rent paid by a non-
business individual isn't a deduction). The simulator records it on
the transaction log and decrements cash; no tax effect.

#### S13.5 — Multiple Bay Area properties at different locations.

Alice owns two properties: a primary residence in
`"san_francisco"` and a rental in `"palo_alto"`. Both are in
California, so both run through `[federal_us, california]` for
income tax — but each location carries its own property-tax rate
(SF County assessor's Prop-13 base vs Santa Clara County's; any
Mello-Roos on the Palo Alto property; SF's city-level real-estate
transfer tax on sale of the SF property vs Santa Clara County's
on sale of the Palo Alto property). The simulator routes property
tax and transfer tax by `location_id`. At year-end, Alice's
federal itemized SALT deduction sums property tax paid across
**both** properties subject to the federal \$10k cap. The
simulator must not double-count, drop a property, or hard-code
the count.

#### S13.6 — Occupancy mode switching over a multi-year timeline.

Alice's house: months 0-23 primary residence, 24-83 rental, sold
month 84. The simulator handles the full timeline:

- Depreciation accrues only months 24-83.
- Carrying costs (property tax, HOA, insurance, maintenance) accrue
  every month regardless of mode (they're owner-paid in both modes).
- Rental income / expense streams accrue only months 24-83.
- §121 eligibility at month 84: requires 24 months of primary-
  residence use within the prior 60 (months 24-83). Alice's primary-
  residence use during that window = 0 months. §121 does not apply;
  full gain is taxable per S13.3's split.

#### S13.7 — One agent owning multiple properties simultaneously.

A separate variant: Alice owns three properties at month 30 — one
primary residence, two rentals at different locations. Each is its
own depreciable-real-property-holding instance with its own location,
its own carrying-cost obligations, its own depreciation schedule
(rentals only), its own occupancy-mode timeline. The engine carries
N rows of the same template and runs each rule against all N rows
in one bulk operation per rule. Adding a 4th property at month 48
is a configuration change: a new property row created at that
month via a configured purchase event.

### Layer 14: Constrained sellability

#### S14.1 — Position with tender-only liquidity.

Alice's position `"alice_private_equity"` is a capital-gains-eligible
holding configured with a `sellability_mask` derived from a market-
model "tender opportunity" path: false except at the specific months
where a tender is offered to the rollout. A floor-triggered sale
policy (S9.1-style) attempts to sell in month M:

- If `sellability_mask[rollout, M]` is true, the sale fires.
- If false, the sale falls through to the next funding source per
  the policy's preference chain (S9.2). The position is not sold;
  the policy is not silently no-op'd — the failure-to-sell-this-
  asset is recorded so the decision log shows that this asset was
  considered and skipped.

#### S14.2 — Mixed-liquidity positions in one scenario.

Alice holds three capital-gains-eligible positions:

- `"sp500_etf"` — sellability_mask = all true (default).
- `"alice_preipo"` — sellability_mask = false until month 36
  (lockup), then true. Represents an IPO lockup.
- `"alice_private_equity"` — sellability_mask sampled from the
  market-model tender path per rollout (S14.1 style).

All three are instances of the same capital-gains-eligible-holding
template. The engine does not have a "PE sale" code path separate
from a "stock sale" code path — sales route through one function
that filters by `sellability_mask[rollout, M]` and skips ineligible
rollouts. Different rollouts may execute the same scheduled sale
on different positions depending on which masks permit it.

## Outputs the simulator must produce

For any run (scenario + market bundle + rollout count + horizon
months):

- **Per-rollout per-month per-agent state**: cash balances by
  account, asset holdings (units + basis + current market value) by
  asset, liability balances (principal + monthly interest accrued +
  monthly principal paid) by liability — addressable for any
  `(rollout, month, agent, entity-id)` tuple. Long-form vs wide-form
  is an implementation choice; the constraint is that any concrete
  query (one rollout's full state at month M, one agent's net worth
  series across months, an asset's units across rollouts) is
  cheap and obvious to express.
- **Transaction log**: every transaction recorded chronologically with
  (rollout, month, kind, parties, amounts, cause-id). Causes link
  back to the policy or obligation that produced the transaction.
- **Per-rollout net worth time series**: derivable from the state
  frames as cash + asset market value − liability principal, summed
  over the agent's holdings. Exposed as a first-class output because
  it's the primary chart consumer.
- **Per-year tax computation breakdown**: per (rollout, year, agent)
  the income totals, deduction amounts, bracket walks, LTCG bracket
  walks, NIIT calculation, federal/CA totals. Enough to audit any
  rollout's tax math.
- **Per-obligation lifecycle**: every obligation (mortgage payment,
  property tax, quarterly tax, year-end tax, HOA, special assessment,
  outside rent, monthly spend) with its accrual month, amount due,
  amount paid, settlement month(s), unpaid balance.
- **Lot disposition log**: every sale of a capital-gains-eligible
  position emits one row per consumed lot, carrying `(rollout,
  month, agent_id, position_id, lot_id, units_sold, proceeds_usd,
  basis_consumed_usd, realized_gain_usd, holding_period_days,
  tax_classification)`. Sales that consume two lots emit two rows;
  the lot identity carries through from the lot's acquisition.
  This is the audit trail for every tax-relevant capital event and
  the primary input to per-month tax allocation.
- **Failure-event log**: every unfunded required obligation emits a
  row carrying `(rollout, month, obligation_id, obligation_type,
  amount_due_usd, amount_paid_usd, shortfall_usd, attempted_funding_
  sources)`. The same agent can recover in a later month (see
  S11.3) — failure-event rows do not retroactively delete; they
  record the moment.
- **Rollout status**: active vs failed, with failure month and the
  obligation that triggered the failure (joinable to the failure-
  event log).

These outputs are projections of the state series and transaction log;
they are not maintained alongside as separate state.

## Non-goals

- **Per-rollout policy parameter divergence.** Policies are scenario-
  level configuration; two rollouts of the same scenario apply the
  same policies. Per-rollout variation in market paths produces
  divergent trajectories naturally.
- **Within-month sub-stepping.** Within a single month, the ordering
  of operations is fixed and documented (mark-to-market → scheduled
  cashflows → required obligations → discretionary policies →
  end-of-month accruals); there is no event-driven simulation within
  the month.
- **Inflation modeling, currency conversion, or non-USD assets.**
  Single currency. Inflation, if relevant, lives in the market model
  (e.g. growing rent / property-tax paths) rather than as a separate
  layer.
- **Liquidity, slippage, transaction costs above what scenarios
  configure as explicit closing costs.** Sales execute at the
  mark-to-market price.
- **Behavioral / regret modeling.** Agents follow their configured
  policies deterministically.
- **Joint filing (MFJ / MFS).** The tax-paying-agent unit covers
  single-filer scenarios. Married-filing-jointly and married-
  filing-separately are not modeled. Filing-status values exist
  (filer agents pick single or head-of-household, per S7.4), but
  the joint-return wiring — two agents sharing one tax return,
  combined brackets, MFJ standard deduction — is out of scope.
  If a scenario wants to model a couple, both agents file as
  single (or HoH where applicable).

## Resolved decisions

- **Lenders are agents for bookkeeping, not for tax.** The lender
  side of an inter-agent loan (e.g. "Bank Bob" in S8) is a
  first-class agent for the purpose of double-entry bookkeeping —
  the loan principal sits on the lender's books as a receivable
  asset, monthly interest income lands on the lender's books, and
  cash flows are recorded symmetrically — but the lender's books
  are a **sink**: no tax accrual fires on the lender, and the
  lender is assumed to have unbounded cash for the purpose of
  origination. The lender's "net worth" / "income" outputs may be
  computed (they fall out of the same machinery) but are not the
  point of the simulation. Bank Bob's interest income does NOT
  generate a tax-liability instrument; the year-tax computation
  template just doesn't apply to lender-flagged agents.

  Modeling consequence: agents carry a `tax_payer` flag (or a
  filing-status field whose value is "n/a — not a tax payer" /
  similar). The year-tax template runs only over agents with a
  configured tax profile.

- **Year-zero estimated tax: prior-year value is scenario
  configuration.** When the safe-harbor rule kicks in for year 0
  of a simulation, the simulator uses a `prior_year_tax_usd` value
  that the scenario supplies as part of the agent's (or tax
  household's) tax profile. No engine-side fallback (no estimate
  from the configured ordinary income, no "skip quarterlies in year
  0"). If the scenario does not configure it, the engine raises at
  scenario-validation time — the user has to make the call.

## Stretch goals (signs of good design)

These are not critical-path requirements but they are diagnostic.
If the design comes out clean, they fall out for free; if achieving
them requires intrusive engine changes, that points at a smell in
the design that's worth examining before shipping.

- **Tax math applies to any tax-paying agent, not a hardwired
  "primary owner".** The critical floor is that one primary agent
  pays their federal + CA income taxes correctly (S6, S7). The
  stretch goal is that the engine has no special-case path for that
  one agent: the year-tax template (see [Asset templates and rule
  routing](#asset-templates-and-rule-routing)) runs once per
  (rollout, year), grouped over every agent the scenario marks as
  tax-paying. If the scenario configures two tax-paying agents,
  both get year-tax computations, both get quarterly + year-end
  obligations, both settle from their own cash + funding chains —
  with no engine-level changes. If achieving this requires more
  than scenario configuration, the design has a `primary_owner`
  hardcoded somewhere it shouldn't be.

  This stretches naturally to a hypothetical scenario where Alice
  and Auragon are both individual tax-paying agents in the same
  rollout (each with their own filing status, own income streams,
  own holdings, own deductions). Owner-plus-partner property
  scenarios should not need to invent a special "second agent
  pathway" — the second agent is just a second `tax_payer=true` row
  in the agents frame.

  Single-agent tax math is **critical**; multi-agent tax math is
  **stretch**. If multi-agent doesn't work but single-agent does,
  that's a shippable state; the engine's structure should make
  multi-agent a small follow-up, not a redesign.

- **Adding a new inter-agent loan instance is configuration.**
  Today the only inter-agent loan type the simulator must handle is
  a mortgage between an individual (Alice) and a lender (Bank Bob).
  The amortizing-loan template (see [Asset templates and rule
  routing](#asset-templates-and-rule-routing)) is written to cover
  ANY fixed-payment debt between two agents — the same code path
  serves an intra-family personal loan, a partner-equity loan, a
  car loan, an HELOC, etc. The stretch goal is that adding such a
  loan to a scenario is **scenario configuration**, not engine
  code: pick the template, supply the principal / rate / term /
  borrower / lender / deductibility-flag parameters, and the loan
  works.

  If adding "Alice borrows \$20k from her mom" to a scenario
  requires touching engine code, the amortizing-loan template
  isn't generic enough.

- **Partner equity / co-ownership of an asset.** The existing legacy
  engine has a "partner equity accrual" feature: a second agent
  contributes a fixed monthly cash amount toward a shared property
  and builds up an equity stake over time per a configured
  equity-per-dollar-contributed formula; at sale, the partner
  receives their share of net proceeds. This is genuinely
  multi-agent — two agents have stakes in one property — and ties
  the multi-agent-tax stretch goal (above) to a concrete scenario.

  Critical floor: single-owner properties work. Stretch: a property
  can carry a "co-ownership ledger" — rows on a `property_stake`
  frame, one per (agent, property) pair — that tracks each agent's
  contribution + equity share over time. Sale proceeds split across
  the ledger. The depreciable-real-property template already
  references a stake column (per [Asset templates and rule
  routing](#asset-templates-and-rule-routing)); making it
  multi-row-per-property is the stretch.

  If achieving this requires inventing a separate "partner
  contribution" obligation pathway with its own state matrices and
  its own settlement logic (which the legacy engine has), the
  generic obligation + recurring-cash-transfer machinery isn't
  generic enough — partner contributions should be a recurring
  transfer between two agents, recorded against the partner's
  stake row at the receiving agent, settled via the contributing
  agent's funding chain like any other recurring obligation.

  Lower priority than the multi-agent tax stretch above; landing
  this without it requires the partner stake but not the partner's
  own tax computation (which simplifies somewhat, since the
  partner is effectively a non-tax-paying co-owner in single-
  primary-agent scenarios).

## Open questions

These are still unresolved and need a decision before the relevant
layer lands. They aren't blocking the earlier layers.

- **Agent-to-agent gifting tax treatment.** S1.2 establishes that
  transfers can carry an income classification. Gift tax,
  exclusions, lifetime exemption — out of scope here, or modeled?
