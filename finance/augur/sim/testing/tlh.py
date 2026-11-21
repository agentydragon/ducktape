"""Stipulated TLH assumptions for tests whose subject is not the harvest model."""

from finance.augur.sim.tlh import TlhAssumptions

# No modeled harvest: a managed sleeve's value moves only with its index, contributions and its own actions.
NO_HARVEST = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)
