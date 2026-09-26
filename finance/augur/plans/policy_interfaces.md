# Actor-facing policy interfaces: remaining extensions

The settled contract (one batch-shaped policy, one ordered action list per actor per
month, fatal rejection with the successful prefix kept, no retry, helpers that only
propose) is in <../SPEC.md> and <../docs/spending_model.md>, implemented by
`sim/{actions,observations,world,session}.py` and `policy/sleeves.py`. The
[roadmap](roadmap.md) owns dependencies. This note keeps the extensions not yet built.
The types below are sketches, not API declarations; extend existing domain types only
for a supported consumer.

The boundary is economic agency: a policy sees information available to its actor and
requests actions that actor could take; the environment owns contracts, execution and
consequences. Rule-driven brokers, lenders and tax authorities suffice; this does not
require a strategic many-agent economy.

## `observations.py`

```python
"""Read-only information available to an actor at a particular time."""

@dataclass(frozen=True)
class Observation:
    at: datetime
    accounts: tuple[AccountView, ...]
    positions: tuple[PositionView, ...]
    contracts: tuple[ContractView, ...]
    claims: tuple[ClaimView, ...]
    market: MarketView
    events: tuple[ActorEvent, ...]
```

Beyond today's cash, lots, pools, held bonds, TLH statements, claims, CPI, tax
records and last month's receipts: accounts distinguish available cash from unsettled
proceeds; contracts expose known terms, amounts and due dates, not a funding strategy;
market observations carry publication/observation times, and a property estimate is
not an observable true value; events convey new information. No future realized paths or
another actor's private books.

## `actions.py`

Housing adds concrete actions such as accepting a mortgage offer or purchasing
property on specified terms (HOUSE, after GHOUSE); the actor cannot declare that a
lender granted a loan. Private-equity responses follow the GPE gate in the roadmap.
Autopay or delegated liquidation requires an explicit standing instruction with
modeled terms. Non-mutating tax/trade previews reuse canonical calculations with
observable inputs and explicit assumptions, never the future realized path.

## Batch layout

`Batch` leaves row/column layout, ragged actions/lots and chunk size undecided. GL may
compare layouts and scalar-to-batch adapters when an actual large-N workload needs it;
it cannot add a second policy interface.
