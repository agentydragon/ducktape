"""Currency percentile interpolation must preserve individual integer quanta."""

import numpy as np

from finance.augur.sim.quantiles import currency_quantiles


def test_currency_quantiles_preserve_int64_precision_and_round_half_up() -> None:
    # These values exceed float64's consecutive-integer range; interpolation must
    # retain the individual quantum between them.
    samples = np.asarray([9_007_199_254_740_993, 9_007_199_254_740_995], dtype=np.int64)
    assert currency_quantiles(samples, (50.0,)) == (9_007_199_254_740_994,)
    assert currency_quantiles(np.asarray([0, 1], dtype=np.int64), (50.0,)) == (1,)
