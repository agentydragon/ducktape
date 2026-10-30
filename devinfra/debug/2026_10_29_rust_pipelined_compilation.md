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

Revisit if debundle PR feedback does not move in the next ci-reports window, or
if a proc-macro or build-script crate misbehaves under metadata-only compiles.
