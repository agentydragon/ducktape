# Reduced-form TLH portfolios

`sim/tlh.py` implements a Python-owned approximation of an index-tracking
portfolio's tax-loss harvesting. The component owns its cohorts' exposure, adjusted
basis and holding periods. Each rollout gets a separate instance.
The household sees value and reported tax basis, and chooses contributions,
gross-cash withdrawals or liquidation.

The losses are modeled assumptions, not reconstructed constituent sales.
Accounting reconciliation does not establish fidelity to a direct-indexing
provider, wash-sale rules or actual harvested holding periods. No constituent
market, cross-account wash-sale interaction or calibrated forecast is implied.

## Component and accounting boundary

`TlhOpening` imports what a direct-indexing statement reports per tax lot: value at
the opening mark, already-adjusted basis and purchase month. It does not require
cumulative historical harvesting. An empty portfolio can receive its
first contribution without an invented opening position. `TlhAssumptions` selects
the gross-loss curve and the modeled short-term fraction.

The component operations are:

- `observe()` returns current value and reported tax basis. It does not disclose
  mutable cohorts or harvesting memory.
- `advance(market)` updates the current mark and returns signed modeled ST/LT
  realizations. It runs once per month, independently of household decisions.
- `contribute(amount)` adds a cohort worth exactly `amount` at the current mark.
  A worthless index takes no contribution.
- `withdraw(gross_amount)` sells exactly `gross_amount` of exposure and returns
  it with the associated realizations. It does not promise that amount after taxes.
- `liquidate()` disposes of all exposure and returns all remaining cash and
  realizations, including any remaining basis at a zero mark.

Money is integer currency quanta. Exposure is not: a cohort holds an exact
rational number of units of the index level, so it is worth `exposure × price`
at any mark and follows the index ratio without rounding. Money is rounded once,
where it leaves the component: a mark, a sale's proceeds, a distribution. There is
no share grid for a policy to size against and no cash kept back inside. Ordinary
holding sales cannot separately dispose of these positions. Portfolio value is
counted once, not both as component value and as ordinary holdings.

The component does not calculate household tax or mutate household cash. The
accounting engine applies its cash and income effects. It can retain financial
statements for valuation and reporting, but not another mutable cohort/basis book.
For a contribution, redemption or modeled harvest, the settlement check is:

```text
cash received by household + change in component reported tax basis
    = realized short-term gain + realized long-term gain
```

Distributions additionally carry their declared income character and contribute
to the income side of that identity. A distribution pays `rate / price × value`
on the portfolio's exposure, rounded once to currency quanta; the existing
declared income treatment is not an assertion of qualified-dividend fidelity.

## Timing and failure

Advance the component before publishing the month's household observation and
before applying any investor contribution or redemption, including scheduled
ones. Month zero uses the opening mark as its previous mark, so its drawdown
input is zero. This coarse monthly convention includes a baseline harvest on
opening positions; it does not replay their pre-simulation history.

A contribution made after observation first participates in the next month's
harvesting. Closing-report valuation can use the closing mark without advancing
the component or realizing the next month's losses early.

The policy emits one ordered action list. The Python runner operates on a
candidate copy of the component and adopts it only after its financial effects
are accepted. A rejected investor action stops that rollout, preserves earlier
successful effects, and does not call the policy again. A later failed payment
does not undo the month's already-applied harvesting. Stopped paths receive no
further model updates.

Expected affordability failures are checked before executing the component.
Invalid component arguments use built-in exceptions; an unexpected model or
accounting exception is not silently classified as investment ruin.

## Approximation and numerical conventions

The model evaluates loss capacity separately for each internal cohort using its
current embedded gain and the index drawdown. Losses reduce that cohort's basis;
they cannot reduce it below zero. New contributions do not inherit another
cohort's past basis reductions. The curve's yields are gross realized losses,
not tax savings. Household tax accounting determines the tax consequences.

Redemptions use internal FIFO. A partial sale of a cohort takes basis in
proportion to the value it sells; final liquidation consumes the remainder. The sale
character uses Augur's monthly convention of long-term at twelve months. The
harvested character is an explicit approximation, not inferred constituent history.

A sale's per-cohort proceeds are the rounded running total less earlier fills, so
they sum to the order's cash exactly, and splitting a sale does not change the cash
it delivers. Basis is rounded per sale, so a split can move a quantum of gain between
sales; liquidation consumes whatever basis is left. A cohort a statement reports at zero value holds no
exposure and keeps its basis until liquidation, which is also how zero-value
exposure is disposed of. Zero-amount requests are no-ops.

The session admits zero prices for series used exclusively by TLH portfolios.
A series also used by ordinary security holdings or trading pools retains their
positive-price requirement. Negative prices are invalid in either case.

`sim/tlh_test.py` pins loss/basis conservation, imported adjusted basis,
contribution isolation, partial and final redemption, zero-value liquidation,
split-sale exactness, invalid requests and candidate-state isolation. These are
accounting controls on stipulated inputs, not empirical calibration evidence.
