# rules_rust pipelined compilation: measured, adopted on a marginal gain

The 2026-10-29 ci-reports latency snapshot ranked
`--@rules_rust//rust/settings:pipelined_compilation=True` as a candidate for the
debundle PR class, whose critical path is a serial first-party rlib chain
(≈145–185 s) ahead of `//devinfra/js/debundle/e2e:conflict_diagnostics_scale_test`.

Matched measurement on devel `0b0dd641`, two cold `bb remote` runners
(`runner-recycling-key` `rust-spike-cold-*`), `--noremote_accept_cached`, target
`//devinfra/js/debundle/e2e:conflict_diagnostics_scale_test` built only:

|                                     | baseline [052274b3](https://app.buildbuddy.io/invocation/052274b3-76a7-4ca6-8ea8-d7ff92fb74c9) | pipelined [e4370ede](https://app.buildbuddy.io/invocation/e4370ede-46c8-4ae0-ae67-d1b93664baf0) |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Elapsed                             | 682 s                                                                                          | 653 s                                                                                           |
| Critical path                       | 610 s                                                                                          | 536 s                                                                                           |
| First-party crates on the path (17) | 266 s                                                                                          | 251 s                                                                                           |
| Third-party crates on the path      | 297 s                                                                                          | 243 s                                                                                           |
| Actions                             | 2,671                                                                                          | 3,056                                                                                           |

The first-party chain shrinks 6%, less than the per-crate host variance between
the two runs (`analysis` 50 s vs 28 s, `selector_runtime` 12 s vs 23 s); these
crates' cost is front-end work that `.rmeta` emission does not skip, so the
critical-path gain comes mostly from the third-party closure. PR CI has that
closure cached, so the expected saving is ≈10–20 s per debundle PR for +14%
actions. Adopted anyway: every measured direction was favourable and the full
debundle closure built with it on. Earlier attempts on a recycled runner
([188d18e3](https://app.buildbuddy.io/invocation/188d18e3-a07a-4785-935d-b70ac19ad15b),
477 s critical path) are unmatched because Skyframe on the recycled runner kept
the baseline's outputs valid regardless of `--nouse_action_cache`.

## Reverted the same day: it broke the aw-importer releases

Adoption shipped as `8c53b49fee` at 2026-10-30T01:04Z. Every `Release
aw-importer` and `Push aw-server` job after it failed -- 47 of 47 attempts,
against 52 of 52 succeeding in the seven hours before. The failing action is
`aw_server_bin`, not a test:

```
ERROR: .../external/aw_server_rust/BUILD.bazel: Compiling Rust bin
       aw_server_bin (1 file) failed
error[E0463]: can't find crate for `aw_server`
  --> aw-server/src/main.rs:10:5
```

Two things kept this out of sight until it reached `devel`. The flag is one
build setting, so it also applies inside the `.bazelignore`d
`third_party/activitywatch` module, and the root `bazel test //...` sweep never
walks into that module (`devinfra/ci/plan_releases.py` records the same gap):
no PR check built these targets.

The per-target `disable_pipelining = True` opt-out that fixed the `@codex`
overlay does **not** fix this: same error with the opt-out on every
`rust_library` in the module ([3dc3bd71]), and the same error without it
([d80bb5a3]); turning the flag off builds and tests clean ([610d4614]).
`aw_server` itself compiles -- only its dependent fails to resolve it, so the
`--extern` path for `aw_server_bin` is still taking metadata despite the
opt-out. The mechanism is unresolved; `bbr build
@aw_server_rust//:aw_server_bin` with the flag set reproduces it, and an
upstream report against rules_rust 0.74 is worthwhile.

The gain was ~10-20 s per debundle PR for +14% actions; the cost was the
aw-importer/aw-server release path. Reverted. Re-adopt together with a PR check
that builds nested-module Rust, e.g. `bbr build
@ducktape_activitywatch//importer:aw_importer_test`.

[3dc3bd71]: https://app.buildbuddy.io/invocation/3dc3bd71-c923-4e8a-8dfb-e58e30567dfd
[d80bb5a3]: https://app.buildbuddy.io/invocation/d80bb5a3-344d-4082-912f-05d2a0e479db
[610d4614]: https://app.buildbuddy.io/invocation/610d4614-6bf1-46b1-86e8-93619520802f
