# Possible-getter batch coverage

**Decision: hold; no PR.** The candidate preserves the frozen pipeline exactly, but three alternating pairs show no consistent CPU or wall-time improvement. The median analysis instruction reduction is about 0.8%, too small to justify the roughly 114 lines of indexed views and batch bitmap logic.

The experiment skips a possible-getter owner only when its ordered read view and rebind view are each covered by earlier completed promotions in the same sorted owner batch. Coverage is local to that batch and keeps the two effect categories separate. The candidate is branch `agent/excalidraw-getter-batch-coverage`, head `a0985ded8b5313c661e780a2a81dbbd11a9cfa63`; it is based on owner-view baseline `b0d446bbd257a8d3de7192b140c3c799dfd94fbc`.

## Correctness and build checks

The candidate check used `-c opt --remote_build_event_upload=all` and these direct binaries plus focused tests:

```text
//devinfra/js/debundle:debundle
//devinfra/js/debundle:debundle_analyze
//devinfra/js/debundle:debundle_project_analysis
//devinfra/js/debundle:analysis_test
//devinfra/js/debundle/e2e:commuting_statements_gate_test
//devinfra/js/debundle/e2e:callback_future_availability_test
```

BuildBuddy invocation `9b7645ae-37b6-4380-b922-d8971f8b91df` pinned the candidate full SHA. Its three direct binary targets used normal optimized `k8-opt` configuration `2b32ee241a5b77b62378230fa58ace0fa849c9b61086c54b75e74227ff903c1a` and were available as BEP artifacts. The test targets used configuration `fdff2a86c709562acb978240a2fcf690816ac653d88917ff8f1b6b5d1e424823`; `commuting_statements_gate_test` executed and passed, while `analysis_test` and `callback_future_availability_test` were cached passes.

The regression test was also overlaid on the unchanged baseline and passed: temporary test-only head `9a74498e2dbb21ee5063661abafbf01fa015840e`, BuildBuddy invocation `424be239-377e-4f14-b208-132e0e75bae5`. That confirms the graph assertions describe existing behavior. The temporary branch was deleted after the check. Commit hooks passed for the candidate commits.

Invocation `926e1a01-685d-436d-8d2c-ea92a44368ea` is excluded: `--run_from_branch` and `--run_from_commit` were placed after the Bazel verb, so Bazel rejected them before running any target. An earlier test iteration expected an `uninitialized_observation`; the emitted graph instead represents this case as a `WriterAfterReader` binding timeline. The final test asserts that timeline and the owner-tagged EagerUse edges, and passes on both baseline and candidate.

## Frozen pipeline comparison

Baseline tools came from BuildBuddy invocation `58c18390-7fed-4d58-9a3d-c3b47b9326b0`; candidate tools came from `9b7645ae-37b6-4380-b922-d8971f8b91df`. Both tool trios use the same `k8-opt` configuration ID above and their respective exact source heads (`b0d446bbd257a8d3de7192b140c3c799dfd94fbc` and `a0985ded8b5313c661e780a2a81dbbd11a9cfa63`).

The three-pair driver was:

```text
python /tmp/agents/2026_11_14/excalidraw-speed/compare-fresh.py \
  /tmp/agents/2026_11_14/excalidraw-speed/merged-opt-tools \
  /tmp/agents/2026_11_14/excalidraw-speed/getter-batch-tools \
  /tmp/agents/2026_11_14/excalidraw-speed/getter-batch-pairs
```

It alternated fresh `project → analyze → apply` runs on the frozen Excalidraw input (`b1,c1,c2,b2,b3,c3`), with unique projection, analysis, and output paths. Each phase used `/usr/bin/time -f '%e %U %S %M' perf stat -x, -e cycles:u,instructions:u,task-clock -- <tool invocation>`. Per-run argv, time, and counter files are in the task scratch directory, not this repository. The workload and frozen input manifest are described in [the pipeline performance note](2026_11_14_excalidraw_pipeline_performance.md).

All six pipelines matched the canonical projection, all 118 emitted JavaScript files, normalized report JSON, and the frozen input manifest. Median analyzer results were:

| Metric            |        Baseline |       Candidate | Change |
| ----------------- | --------------: | --------------: | -----: |
| Wall time         |         11.12 s |         11.78 s |  +5.9% |
| CPU time          |         17.69 s |         18.22 s |  +3.0% |
| User instructions | 144,733,180,795 | 143,583,056,034 | -0.79% |
| User cycles       |  82,828,093,563 |  84,952,540,036 |  +2.6% |
| Peak RSS          |     847,800 KiB |     781,576 KiB |  -7.8% |

Pairwise analyzer wall deltas were `-14.452%`, `-2.326%`, and `+6.835%`; CPU deltas were `+2.996%`, `-2.380%`, and `+5.431%`. Full-pipeline wall times were baseline `[25.09, 15.20, 15.69]` seconds and candidate `[19.09, 14.95, 17.96]` seconds. Three pairs are not enough to establish a wall-time gain, and the candidate's median full-pipeline wall time is higher. Keep the experiment held unless a later route produces a materially larger, repeatable improvement.
