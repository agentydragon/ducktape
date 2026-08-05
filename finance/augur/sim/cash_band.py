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

from typing import NamedTuple

import jax.numpy as jnp


class CashOrder(NamedTuple):
    """What the band asks for this month, in cents, per rollout.

    At most one side is non-zero: a band with `floor <= ceiling` cannot be crossed in both
    directions at once.

    A NamedTuple rather than a dataclass so it is a native JAX pytree — the engine returns
    this from traced code, and a plain dataclass is not a valid jit output.
    """

    raise_cents: jnp.ndarray
    invest_cents: jnp.ndarray


def validate_band_bounds(*, floor_usd: float, ceiling_usd: float) -> None:
    """Check the band's shape at COMPILE time, on the configured amounts.

    It cannot be checked per-month: the bounds may be CPI-indexed, so their monthly values
    are traced arrays, and a traced value cannot drive a Python raise. Validating the base
    amounts is sufficient rather than a compromise — indexing scales both bounds by the
    same series, so an ordering that holds at configuration holds on every path.
    """

    if floor_usd < 0:
        raise ValueError(f"cash band floor must not be negative; got {floor_usd=}")
    if floor_usd > ceiling_usd:
        raise ValueError(
            f"cash band floor must not exceed its ceiling; got {floor_usd=}, {ceiling_usd=}. "
            "An inverted band has no interior, so every balance crosses both bounds and the "
            "policy would sell and buy in the same month, forever."
        )


def cash_order(
    *,
    cash_cents: jnp.ndarray,
    scheduled_outflow_cents: jnp.ndarray,
    floor_cents: jnp.ndarray,
    ceiling_cents: jnp.ndarray,
) -> CashOrder:
    """Size this month's raise or investment from the projected end-of-month balance.

    All arguments are `(rollout,)`. `scheduled_outflow_cents` is what the month is already
    committed to paying, so the decision is made against where cash will actually land
    rather than where it happens to sit before the bills.

    Runs inside the jitted scan, so it validates shapes (static, therefore checkable) but
    never values — `validate_band_bounds` owns that, at config time.
    """

    if not (cash_cents.shape == scheduled_outflow_cents.shape == floor_cents.shape == ceiling_cents.shape):
        raise ValueError(
            "cash_order arguments must share one (rollout,) shape; got "
            f"{cash_cents.shape=}, {scheduled_outflow_cents.shape=}, {floor_cents.shape=}, {ceiling_cents.shape=}"
        )

    projected = cash_cents - scheduled_outflow_cents
    return CashOrder(
        raise_cents=jnp.where(projected < floor_cents, ceiling_cents - projected, 0).astype(jnp.int64),
        invest_cents=jnp.where(projected > ceiling_cents, projected - floor_cents, 0).astype(jnp.int64),
    )
