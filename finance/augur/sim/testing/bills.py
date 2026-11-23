"""Bills for tests whose subject is not deductible spending."""

from functools import partial

from finance.augur.sim.bills import Biller

# Tied to no property and carrying no deduction, so its deductible fraction goes unread.
biller = partial(Biller, property_id=None, deduction_category=None, deductible_fraction_ppb=0)
