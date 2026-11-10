# Excalidraw corpus: roughjs inside the editor chunk dominates Analyze

`DebundleAnalyze` on RBE (opt exec configuration) for
`//devinfra/js/debundle/excalidraw:debundle`, `@excalidraw/excalidraw` 0.18.1,
two bundle shapes built from the same app entry and packages:

| Bundle shape                                                     | Wall    | CPU     | Peak RSS | Invocation                             |
| ---------------------------------------------------------------- | ------- | ------- | -------- | -------------------------------------- |
| roughjs and clsx inlined in the 545 KB shared editor chunk       | 304.2 s | 337.1 s | 939 MiB  | `d40385ac-fd78-4d2f-aabd-cf08f606199b` |
| same, second cold run                                            | 304.9 s | 336.6 s | 942 MiB  | `f8460b4c-136b-4305-babe-278454e9ac8d` |
| same bundle, with the spec's vendor marks and modules            | 340.8 s | 374.3 s | 933 MiB  | `8322fa51-4ee5-4b03-8604-ff0f58bfdd5e` |
| `app/vendor/` entries split roughjs (27 KB) and clsx into chunks | 43.6 s  | 64.9 s  | 988 MiB  | `127bc44a-c144-4049-9901-458854ba1a3f` |
| same, `analysis_threads = 1`                                     | 48.6 s  | 50.2 s  | 808 MiB  | `7ec8041c-790d-4daa-9534-c5385cbae6ae` |
| same as split, engine at `30ebed4616`                            | 39.5 s  | 59.5 s  | 927 MiB  | `8b791218-5164-4e2d-86ee-ef37cccf2160` |

Total JS is unchanged (4,936,831 vs 4,937,119 bytes); the editor chunk loses
72 of 1,859 source units. Moving roughjs (renderer, generator, fillers and its
four helper packages) out of the editor chunk removes about 260 s of Analyze, so
cost is superlinear in some property of that chunk, not proportional to size.
<analysis_serial_phases.md> § Public corpus agrees: the published package build,
where roughjs stays an external import, analyzes in 17–18 s on a 4-core VM.

Not yet profiled. Next action: stage the first bundle shape's inputs (the
corpus at the parent of the commit adding `app/vendor/`) and run the `-c opt`
analyzer under `perf_wrapper.sh` (<../docs/bazel_integration.md> § Profiling),
comparing against the split shape.
