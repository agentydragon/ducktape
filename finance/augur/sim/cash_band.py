"""The cash band: how much the actor raises or invests in a month.

The band's shape is configured here and enforced by `rust/engine/target_allocation.rs`.
What lives in this module is the one check the engine cannot make for itself, and the
statement of the rule it implements.

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

from decimal import Decimal


def validate_band_bounds(*, floor: Decimal | int, ceiling: Decimal | int) -> None:
    """Check the band's shape at COMPILE time, on the configured amounts.

    It cannot be checked per-month: the bounds may be CPI-indexed, so an inverted band
    would otherwise surface as a runtime failure on some paths and not others. Validating
    the base amounts is sufficient rather than a compromise — indexing scales both bounds
    by the same series, so an ordering that holds at configuration holds on every path.
    """

    if floor < 0:
        raise ValueError(f"cash band floor must not be negative; got {floor=}")
    if floor > ceiling:
        raise ValueError(
            f"cash band floor must not exceed its ceiling; got {floor=}, {ceiling=}. "
            "An inverted band has no interior, so every balance crosses both bounds and the "
            "policy would sell and buy in the same month, forever."
        )
