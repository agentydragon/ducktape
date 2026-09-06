"""The numeric mode the simulator's tests assume."""

from __future__ import annotations

import jax

# The simulator's fixed-point accounting is int64 throughout, and jax silently NARROWS int64
# to int32 unless x64 is on — so a $1M order against BTC's satoshi scale (~1e14) wraps to a
# negative quantity rather than failing loudly. `jax_engine` sets it at import and refuses to
# run without it, but the pure policy modules (`target_allocation`, `allocation`, `cash_band`)
# do the same arithmetic without importing the engine. Setting it here means a test of one of
# those computes what production computes, instead of depending on which module happened to be
# imported first. Found by a sweep whose orders came back negative.
#
# Safe after the imports above: jax reads this when an array is first created, not at import.
jax.config.update("jax_enable_x64", True)
