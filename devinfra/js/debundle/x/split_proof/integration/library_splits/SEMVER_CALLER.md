# A caller of the pinned semver wrapper

The readable semver 7.8.6 bundle (SHA-256
`9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44`)
still stops at original ordinal 0 with `unsupported global Object`.
The ordinary full-source halves and advance-second placements refuse.
[The caller trial](semver_caller_test.mjs) extracts the exact factory at
ordinal 6. It supplies a small stand-in for the captured own-name helper and
a public `open`/alias harness. This harness is distinct from the shipped
public semver API; in the full bundle the first direct loader call is already
at module initialization, at ordinal 56.

The authored projection moves the exact factory into a helper module. The
[Lean checker](../../SemverCallerTest.lean) checks the source and independent
artifact reparse, executable ownership and ordered statements, full linked
state, helper capture address, and both public export addresses. It rejects a
hypothetical same-syntax relocation to the decoy helper cell, which contains
the same function object in the initialized state. It proves equality of
initialization for every model world at equal fuel and lifts that equality
through one fixed caller with checked public addresses. The fixed caller uses
the existing `callFnTurn` and opaque `World.talk` conversation. Neither the
ambient own-name operation nor an arbitrary native callback is implemented.

The concrete caller invokes the factory after initialization and obtains the
actual named loader closure. The wrapper chooses its callback normally. The
opaque callback resolves a captured effect helper by cell and reenters the
loader through it: success requires the cache to contain the module and the
same exports before the effect. The helper checks the bare receiver and the
exports alias; it writes and logs. Normal completion caches the same object;
a language throw retains the write, resets the cache and carries the original
sentinel, so retry creates fresh exports and runs the callback again. A model
refusal after the effect keeps the state and skips the catch. The trial compares
the complete modeled outcomes at equal fuel from 0 through 96, including fuel
exhaustion, and checks the concrete successes and throw separately. Node
independently runs both projections with a corresponding reentrant callback.

The next caller obligation is to change the live helper after the loader is
allocated: an uncached call must use the new helper and a cached call must
skip it. The full bundle still needs the actual global bootstrap and the
ordinal 56 initialization call to link to an external API observation.
