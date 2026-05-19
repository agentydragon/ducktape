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

Everything below builds on these.

## Scenarios — bottom up

Each scenario adds one new capability. The simulator must handle all
of them; the order is the order in which the implementation should
acquire support, not a runtime classification.

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

#### S4.4 — Multiple sales in different months, partial lots.

Alice sells \$5000 of stock in month 3, another \$5000 in month 9.
Each sale realizes gain proportional to the basis-per-unit at that
month. The order is preserved on the transaction log.

#### S4.5 — Multiple asset classes.

Alice owns SP500, BTC, and a private-equity position with a
configured liquidity regime. Each holds its own units + basis, each
has its own market price path. Holdings and balances scale row-wise
in the output, not by adding columns.

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

Alice realizes \$10000 from SP500 (long-term), \$3000 from BTC
(long-term), and \$5000 from a private-equity sale (long-term). All
fold into the same long-term-capital-gain bucket for federal tax
purposes, which is summed once and taxed once at federal LTCG rates.
California treats LTCG as ordinary income — the same gains feed CA's
ordinary-income computation. The simulator does not compute tax on
SP500 sales separately, double-count, or miss a source.

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
zero (no prior-year tax data) the simulator's behavior is
deterministic and documented; reasonable choices include "no
quarterly payments emit, year-end true-up covers the full year" or
"user supplies a prior-year-tax knob".

The simulator must:

- Emit quarterly obligations at the right months with the right
  amounts.
- Settle each obligation from Alice's cash, drawing on her funding
  policies (e.g. sell stock to cover) if cash is short.
- True up at year-end: the year-end obligation is `actual_year_tax
  − sum_of_quarterlies`, never producing a payment greater than the
  actual year tax across all five settlement events.

#### S6.5 — Net investment income tax.

Above the federal MAGI threshold (today \$200k single / \$250k MFJ),
an additional 3.8% NIIT applies to investment income. The simulator
applies it correctly — including the cap at `min(NII, MAGI − threshold)`
— and folds it into the year's federal tax.

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

Single, married filing jointly, married filing separately, head of
household. Each filing status pins different bracket boundaries,
standard deduction amounts, and threshold values (NIIT, safe-harbor
high-income). The simulator carries the filing status per agent
(or per tax-household — see open question below) and looks up the
right tables.

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

#### S8.3 — Bank Bob's interest income is taxable.

Bank Bob earns ordinary income on each month's interest portion.
If Bank Bob is modeled as a tax-paying agent (i.e. it has a filing
status and tax profile), then year-end federal + CA tax on that
income is computed and accrued on Bank Bob's books. If Bank Bob is
modeled as a non-tax-paying entity (e.g. an institutional lender out
of scope for tax), the income is still recorded — it's a contract
parameter, not a tax-profile parameter — but no tax accrues.

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

## Outputs the simulator must produce

For any run (scenario + market bundle + rollout count + horizon
months):

- **Per-rollout per-month per-agent state**: cash balances by
  account, asset holdings (units + basis + current market value) by
  asset, liability balances (principal + monthly interest accrued +
  monthly principal paid) by liability. Long-form, one row per
  (rollout, month, agent, entity-id). The cardinality of agents,
  accounts, assets, liabilities goes into row count, not into the
  schema.
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
  property tax, quarterly tax, year-end tax) with its accrual month,
  amount due, amount paid, settlement month(s), unpaid balance.
- **Rollout status**: active vs failed, with failure month and the
  obligation that triggered the failure.

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

## Open questions

These are unresolved and need a decision before the relevant layer
lands. They aren't blocking the earlier layers.

- **Tax household scope.** Tax computation today is single-agent.
  Joint filers with separate cash accounts but a single tax return —
  is the tax computation on an "agent" or on a "tax household"
  abstraction that aggregates one or more agents? Either works; the
  decision affects how filing status, joint deductions, and combined
  brackets are modeled.
- **Bank Bob's existence model.** The mortgage scenarios above
  describe the lender as another agent in the same simulation. An
  alternative is to model lenders as "contract counterparties"
  without their own balance sheet, since simulating a bank's balance
  sheet is usually not what the user wants. The decision affects how
  many agents typical scenarios carry and whether lenders pay tax.
- **Year-zero quarterly estimated tax.** With no prior-year tax data,
  several behaviors are defensible: (a) no quarterlies fire and the
  year-end true-up covers the full year, (b) the user supplies a
  `prior_year_tax_usd` knob, (c) the simulator estimates from the
  scenario's configured ordinary income. The current legacy engine
  picked (a) + (b); the new implementation should commit to a default
  explicitly.
- **Agent-to-agent gifting tax treatment.** S1.2 establishes that
  transfers can carry an income classification. Gift tax,
  exclusions, lifetime exemption — out of scope here, or modeled?
- **Inter-agent loans beyond mortgages.** S8 describes one specific
  contract type. Should the simulator support arbitrary inter-agent
  loans (e.g. partner equity loans, intra-family lending), or is
  mortgage the only template?
