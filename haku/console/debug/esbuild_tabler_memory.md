# haku/console frontend bundle: esbuild OOMs the @tabler/icons-react barrel

Status: **resolved — landed the deep per-icon import (no RAM override needed).**
The barrel's ~8.7 GB esbuild peak has no clean per-action RAM lever (only
platform-global works — see below), so every icon is imported by subpath
(`dist/esm/icons/Icon<Name>.mjs`, default export), which esbuild processes in
~tens of MB. Those subpaths ship no types, so a wildcard ambient declaration in
`tabler_icons.d.ts` types them. `icons.tsx` is the one module that imports them
and it says so at the top. The investigation/memory notes below are retained for
the next time a `@tabler` barrel import tempts someone.

The SPA bundle `//haku/console/frontend:bundle` OOMs esbuild on BuildBuddy RBE
whenever a module imports from the `@tabler/icons-react` _barrel_
(`import { IconMessage2 } from "@tabler/icons-react"` — `feedback.tsx`, since
retired, at the time). Investigation notes below. BuildBuddy source referenced
from a clone at `~/code/buildbuddy`.

## Symptom

```
Splitting Javascript .../main.tsx [esbuild] failed: (Exit 1): launcher failed:
error executing esbuild command ... Error: The service was stopped
  at .../esbuild@0.19.9/.../esbuild/lib/main.js:1083:25
```

Fails after ~95–140s of critical path. esbuild 0.19.9 (pinned via rules_esbuild).

## Confirmed: it's an OOM-kill, and esbuild needs ~1 GB (not multi-GB)

Reproduced locally under a memory cgroup (`systemd-run --user --scope -p MemoryMax=…`)
with the exact esbuild 0.19.9 native binary, bundling an isolated
`import { IconMessage2 } from "@tabler/icons-react"`:

| `MemoryMax` | result                                                     |
| ----------- | ---------------------------------------------------------- |
| 400 M       | esbuild **exit 137** (SIGKILL = "The service was stopped") |
| 1 G         | ✅ succeeds                                                |

Peak RSS measurements (native esbuild, `/proc/<pid>/status` VmHWM):

- Isolated `@tabler` barrel (one icon): **~543 MB**.
- Full app `main.tsx` standalone (top-level pnpm deps only — a SUBSET): peaked
  ~1008 MB before bailing on an unresolved `@mantine/notifications` import.
- **Actual on RBE (authoritative):** `usageStats.peakMemoryBytes` for the esbuild
  action = **8741 MB (~8.5 GiB)** — read from the execution metadata via
  `GetExecution` (raw curl; `bbapi execution` has a proto-version mismatch on
  `invocationLinkType`, but raw JSON is fine).

The local ~1 GB figure was a **subset** (only top-level pnpm deps resolved). The
full rules_js `node_modules` tree (all transitive deps of the `@tabler` barrel +
Mantine + everything) makes esbuild process ~9× more → **8.7 GB peak**. The exact
bazel action args (`bundle_esbuild.args.json`) confirm **no `--splitting`**, so
metafile/minify don't change this.

## The BuildBuddy memory mechanism (traced in source)

The firecracker microVM RAM is the ceiling (cgroup hard-limit is OFF by default:
`ociruntime/ociruntime.go:92` `executor.oci.enable_cgroup_memory_limit=false`):

- `firecracker.go:598` → `MemSizeMb = TaskSize.EstimatedMemoryBytes / 1e6`.
- `effectiveSize = getMostAccurateTaskSize` (`scheduler_server.go:520`):
  **Measured → Predicted → naive**, where **Measured is disabled for Firecracker**
  (`tasksize/tasksize.go:178`).
- The naive base = `Override(Default, Requested)` (`execution_server.go:966`) — so
  exec_properties **are** applied to the base size. `Default` for our platform
  (`tasksize.go:Default`) = `400 MB` + `200 MB` (firecracker) + `800 MB`
  (init-dockerd) ≈ **1.4 GB**, shared with dockerd/node/OS.
- The **ML model predictor can override** the requested size (Predicted > naive).
  Disable with exec_property `debug-disable-predicted-task-size=true`
  (`server/util/platform/platform.go:97`, `tasksize_model.go:203`).
- **GOTCHA:** the memory exec_property key is `EstimatedMemory`
  (`platform.go:171`), **not** `EstimatedMemoryBytes`. (Disk is `EstimatedFreeDiskBytes`,
  CPU is `EstimatedComputeUnits`; memory is the odd one out.)

## RBE experiments — the override DOES work; esbuild just needs 8.7 GB

| change                                                                     | result                                                  |
| -------------------------------------------------------------------------- | ------------------------------------------------------- |
| platform `EstimatedMemoryBytes=8GB` (**wrong key**)                        | OOM (key unrecognized — no effect)                      |
| platform `EstimatedMemory=8GB` + `debug-disable-predicted-task-size=true`  | esbuild peaked **8741 MB**, OOM (8 GB short by ~0.7 GB) |
| platform `EstimatedMemory=16GB` + `debug-disable-predicted-task-size=true` | ✅ **builds + all tests pass**                          |

The override (correct key `EstimatedMemory` + disable the model so the request
wins) reaches the VM — proven by the 8741 MB peak (impossible on the ~1.4 GB
default). It just had to exceed esbuild's 8.7 GB need.

Note: action-level exec_properties via `--modify_execution_info` do **not** reach
the memory sizing — confirmed empirically: a fresh (`--noremote_accept_cached`)
run with `--modify_execution_info=esbuild=+EstimatedMemory=16GB
+debug-disable-predicted-task-size=true` still OOM'd (esbuild capped at the
model's ~7.6 GB default prediction). Only **platform** `exec_properties` flow into
the memory sizing BuildBuddy uses. Combined with `rules_esbuild`'s `esbuild()`
not exposing an `exec_properties` attr (per-target), the override **cannot** be
scoped to the esbuild action or mnemonic — only platform-global.

The Bazel `modify_execution_info` comma-combined form (`+a=1,+b=2`) is also
rejected as "malformed"; use two separate flags (both parse fine) — but they still
don't reach memory.

## Why per-target / per-mnemonic is blocked (tried all three)

1. **`--modify_execution_info` (per-mnemonic):** doesn't reach BuildBuddy's memory
   sizing (see above) — fresh 16 GB mnemonic-scoped run still OOM'd.
2. **Patch `rules_esbuild` + forward to `ctx.actions.run`:** `ctx.actions.run()`
   has **no `exec_properties` parameter** in this Bazel version
   (`run() got unexpected keyword argument 'exec_properties'`).
3. **Use the built-in `exec_properties` rule attr** (it IS built-in — declaring a
   custom one named `exec_properties` fails as "built-in attributes cannot be
   overridden"): setting it on the bundle target forwards to the action, **but**
   triggers a Bazel action conflict — `main.tsx`'s `copy_to_bin` action is
   generated under two exec configs (bundle_esbuild with the override vs `tsc_test`
   without), producing "file main.tsx is generated by these conflicting actions."

So per-target override is genuinely blocked by Bazel/BuildBuddy mechanics; the
barrel works only via the platform-global override.

## Caveat: the working config is global

Setting `EstimatedMemory=16GB` + `debug-disable-predicted-task-size=true` on the
`rbe_linux_x64` platform makes **every** RBE action claim 16 GB and disables the
task-size model for all of them. That hurts CI (16 GB/action caps concurrency;
no model right-sizing). It works for this one esbuild action but is a blunt
instrument.

## Alternatives (avoid the global cost)

- **Deep per-icon import** (`@tabler/icons-react/dist/esm/icons/IconMessage2.mjs`):
  esbuild processes one icon (~tens of MB), fits the default VM, no platform change.
  Needs a small ambient `declare module` for tsc (no `.d.mts` for the subpath).
  This is what `x/rspcache/admin_ui` does (it has no tsc gate).
- **Inline SVG**: drop `@tabler`; render the message-2 paths inline. No dependency,
  no RAM issue, passes esbuild + tsc.

## Artifacts

- esbuild 0.19.9 native binary fetched to `/tmp/claude/esbuild-mem/package/bin/esbuild`.
- BuildBuddy source cloned to `/home/agentydragon/code/buildbuddy`.
