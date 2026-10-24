# Unused cross-module purity inference

Profiling the exact full consumer action, including its package inputs and
transforms, found a separate bottleneck before main chunk analysis. About 64%
of the first minute's sampled time was in `ModulePurityFacts::prepare`, almost
entirely `ChunkValueFlow::analyze`. Success on the reduced raw chunk therefore
did not establish full-spec performance.

The oracle previously prepared every module in the static import closure,
even if none of its export verdicts could contribute to the returned imported
purity maps. The optimization collects all named/default import targets first,
validates all asserted axioms, and prepares only providers with a consumed,
unasserted export. Namespace member calls remain outside this oracle. Every
import in every module contributes to the required set, so transitive fixpoint
dependencies are retained.

The neutral CLI regression uses namespace imports of four providers with broad
object alias/property flow. The callback-fixed binary at `c3dd94118c` exceeded
60 seconds before main analysis on this shape. Skipping unused inference lets
it complete while preserving the original and emitted runtime result. Existing
purity E2E tests cover transitive inference, impure demotion, namespace handling,
and author assertions.

This optimization does not finish the real consumer's performance repair:
the exact full action still reaches expensive value flow in a provider with
consumed exports. That path needs a separate fix; no gate success is claimed.
