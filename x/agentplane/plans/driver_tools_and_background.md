# Driver-provided tools and background work on the seam

Status: **decisions pending**. The provider evidence is settled and lives in
[`../docs/driver_tools.md`](../docs/driver_tools.md) and
[`../docs/background_work.md`](../docs/background_work.md); what is open is whether the runner
protocol carries either surface, and in what shape.

1. **Decide whether the runner exposes driver-provided tools at all.** Nothing above the seam asks
   for them yet. If it does, the shape question is settled by Codex: either the declared tool set is
   immutable for the life of a session, or the runner offers a replace verb that is unsupported on
   Codex. That choice is product-visible, because a session serves one Thread.
2. **Decide whether the runner exposes background work.** The floor both harnesses reach is list +
   stop by harness id, correlated to the originating tool call. Everything richer is Claude-only and
   stays native.
3. **Cover whichever surface lands with scripted tests against both binaries.** Neither
   `harness_tests/claude` nor `harness_tests/codex` exercises driver-provided tools or background
   work today; the evidence behind the two docs came from a throwaway rig.
