"""The cash band: how much the actor raises or invests in a month.

Pure math, integer cents, every array carrying the rollout axis — the companion to
`allocation.py`, which decides which sleeves the amount comes from or goes to.

## The rule

Cash is kept inside `[floor, ceiling]`. Cross a bound and you go to the FAR edge:

- below the floor -> sell up to the ceiling
- above the ceiling -> invest down to the floor
- inside -> do nothing

That is an (s,S) inventory policy, and the far edge is the point of it: it minimizes the
number of crossings, and each crossing is a trade. Two reasons that matters more here than
tidiness. Every sale is a taxable event. More importantly, a thin buffer makes the agent a
FORCED SELLER into every dip — which is the risk the whole allocation exercise exists to
price, so a policy that manufactures it would flatter every portfolio that avoids it.

The honest counterargument, recorded rather than resolved: refilling to the ceiling
realizes gains earlier than refilling to the floor would, and deferral is worth real
money. Far edge versus near edge is an empirical question this simulator can answer, and
it is the first rule to vary if results turn out sensitive to it.

## Timing

The decision is made ONCE, at the start of the month, against the balance the month is
projected to end at — cash minus the obligations already scheduled for it. Obligations are
scheduled, so that projection is a calculation, not a forecast.

Deciding once is what makes the agent behave like a person rather than a machine that
trades twice a month, and deciding BEFORE obligations settle is what makes a later failure
mean something: an unpayable obligation then means there was genuinely nothing left to
sell, rather than that the sale had not been attempted yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class CashOrder:
    """What the band asks for this month, in cents, per rollout.

    At most one side is non-zero: a band with `floor <= ceiling` cannot be crossed in both
    directions at once.
    """

    raise_cents: NDArray[np.int64]
    invest_cents: NDArray[np.int64]


def cash_order(
    *,
    cash_cents: NDArray[np.int64],
    scheduled_outflow_cents: NDArray[np.int64],
    floor_cents: NDArray[np.int64],
    ceiling_cents: NDArray[np.int64],
) -> CashOrder:
    """Size this month's raise or investment from the projected end-of-month balance.

    All arguments are `(rollout,)`. `scheduled_outflow_cents` is what the month is already
    committed to paying, so the decision is made against where cash will actually land
    rather than where it happens to sit before the bills.
    """

    if not (cash_cents.shape == scheduled_outflow_cents.shape == floor_cents.shape == ceiling_cents.shape):
        raise ValueError(
            "cash_order arguments must share one (rollout,) shape; got "
            f"{cash_cents.shape=}, {scheduled_outflow_cents.shape=}, {floor_cents.shape=}, {ceiling_cents.shape=}"
        )
    if np.any(floor_cents > ceiling_cents):
        raise ValueError("cash band floor must not exceed its ceiling")
    if np.any(floor_cents < 0):
        raise ValueError("cash band floor must not be negative")

    projected = cash_cents - scheduled_outflow_cents
    return CashOrder(
        raise_cents=np.where(projected < floor_cents, ceiling_cents - projected, 0).astype(np.int64),
        invest_cents=np.where(projected > ceiling_cents, projected - floor_cents, 0).astype(np.int64),
    )
