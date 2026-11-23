# Combined optional-callee proof memory

The fixture combines nullish methods, a throwing getter, a noncallable method and a null receiver in one initializer. Node exports `gta`. Strict emission reached Lean proof checking, where the process exited with status 137. The focused semantic tests exercise these conditions independently. [Original BuildBuddy run](https://app.buildbuddy.io/invocation/b9585af4-d881-45de-9765-5a1e60d38d1e)

Run from the repository root with `SPLITTER` pointing to the native `lowersplit` launcher and `WORK` set to an absolute scratch directory.

```bash
kit=debug/optional_callee_proof_memory
mkdir -p "$WORK/tmp"
jq -j '.source' "$kit/fixture.json" > "$WORK/source.mjs"
jq '.spec' "$kit/fixture.json" > "$WORK/spec.json"
node --input-type=module -e 'console.log((await import(process.argv[1])).result)' "$WORK/source.mjs"
TMPDIR="$WORK/tmp" "$SPLITTER" "$WORK/source.mjs" "$WORK/spec.json" "$WORK/out" --require normal-completion
```

The generated certificate used witness fuel 32. On a BuildBuddy runner with `lean -j1`, the declarations preceding the execution theorem elaborated in 1.71 seconds and used 1,301,268 KiB of peak resident memory. The complete certificate crossed a 2,450 MiB RSS guard with both proof tactic orders.

| First tactic | Time to guard |      Peak RSS |
| ------------ | ------------: | ------------: |
| `rfl`        |        6.26 s | 2,528,336 KiB |
| `cbv`        |        4.45 s | 2,542,040 KiB |

The run uploaded the generated Lean certificate and profiler logs. [BuildBuddy profile](https://app.buildbuddy.io/invocation/76620404-a32a-4f60-937d-b0daa4851899)

`cbv` crossed the same guard on each obligation alone. Source-side normal completion reached 2,514,348 KiB in 6.17 seconds. Source/candidate equality reached 2,532,520 KiB in 4.86 seconds. `rfl` alone did not prove normal completion. [Isolated proof obligations](https://app.buildbuddy.io/invocation/e0f904f0-148d-4c2b-ae76-7bcca56e1caf)

Using `LinkedBehavior.of_runs` with separate reductions to the same final state reached 2,544,716 KiB in 6.36 seconds. [Separate reductions](https://app.buildbuddy.io/invocation/a192a0a2-a2f9-40c3-be1d-146bb40dc816)

Fuel 26 was the smallest bound at which both programs completed normally in the probe world. The full proof at fuel 26 with `cbv` first reached 2,519,264 KiB in 6.97 seconds. The fixture still exceeds the proof memory budget. [Smallest probe fuel](https://app.buildbuddy.io/invocation/a56ce423-6ee5-45e3-9d32-bb0f3da69dde)
