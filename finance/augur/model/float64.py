"""JAX samples in 64-bit here, because a sampled level becomes money.

JAX defaults to 32-bit and narrows silently. float32 carries about seven significant
decimal digits; a currency level in the hundreds of thousands with cents needs nine, so a
$600,000.007 home value samples as $600,000.00 and `sampled_array_to_quanta` then records
that figure exactly. The loss is in the sample, not the conversion.

Importing this module sets the flag, which has to happen before the process builds its
first array -- so every sampler whose output crosses into money imports it, rather than
one entry point setting it and the rest inheriting it by sharing a process. That inherited
arrangement is what was here before: the JAX engine set it at import and refused to run
without it, and the samplers were only ever correct because it had already run.
"""

from __future__ import annotations

import jax
import numpy as np

jax.config.update("jax_enable_x64", True)

# The dtype a sampled level crosses to the compiler in.
LEVEL_DTYPE = np.float64
