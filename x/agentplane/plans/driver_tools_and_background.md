# Driver-provided tools and background work on the seam

Status: **decisions pending**. The provider evidence is settled and lives in
[`../docs/driver_tools.md`](../docs/driver_tools.md) and
[`../docs/background_work.md`](../docs/background_work.md); what is open is whether the runner
protocol carries either surface, and in what shape.

1. **Decide whether the runner exposes driver-provided tools at all.** Nothing above the seam asks
   for them yet. If it does, two sub-decisions follow. Carry a per-tool deferral flag on the
   declaration and leave what the model sees each turn to the harness, since both already do it
   through their own tool search. And pick a side on re-describing a tool: either the declared set is
   immutable per session, or the runner offers a replace verb and names it unsupported on Codex,
   whose schemas are fixed for the life of a thread.
2. **Decide whether the runner exposes background work.** The floor both harnesses reach is list +
   stop by harness id, correlated to the originating tool call. Everything richer is Claude-only and
   stays native.
3. **Cover whichever surface lands with scripted tests against both binaries.** Neither
   `harness_tests/claude` nor `harness_tests/codex` exercises driver-provided tools or background
   work today; the evidence behind the two docs came from a throwaway rig.
