# Visual publisher on the Bazel runner

The opt-in `visual-colocation` mode of `bench-remote-bazel.yml` runs a remote
test alongside the existing released publisher on one GitHub runner. Normal CI
and publication remain separate. The probe republishes an already completed
same-repository PR run, while its PR head executes only on BuildBuddy; it does
not publish artifacts from that new test invocation.

## Hosted proof, 2026-10-31

[Run 41187144077](https://github.com/agentydragon/ducktape/actions/runs/41187144077)
passed at workflow source `b6b0d0d114b9f2df4b0b2132a40b884c135d1b65`.
The host checked out trusted `devel` at
`ff579d3582ac18a76a8d4963ccd8a1e1da88414f`; its Nix closure provided the released
publisher. The selected completed upstream was
[CI 41180891261, attempt 1](https://github.com/agentydragon/ducktape/actions/runs/41180891261),
PR head `83cbe67dcb8b50ce306353520660951dc79de286`.

| Work                                      | GitHub step interval, UTC | Duration |
| ----------------------------------------- | ------------------------- | -------: |
| Fresh remote publisher unit test          | 09:04:44–09:05:10         |     26 s |
| Re-publication of completed visual review | 09:04:44–09:05:29         |     45 s |
| Parallel group                            | 09:04:44–09:05:29         |     45 s |

The job occupied one runner from 09:04:15 to 09:05:29, including checkout and
tool setup. The remote command first asserted that AWS credential variables and
the publisher-only test sentinel were absent, then ran the real
`//devinfra/pr_visuals:test_publisher` target with fresh test results and normal
RBE/cache settings. Its
[Bazel invocation](https://app.buildbuddy.io/invocation/3af53d5c-1515-4ce0-b6ab-67eeaa333706)
passed. These checks show the tested environment did not receive those variables;
they are not a complete credential-isolation audit.

The existing review completed successfully with nine targets: 13 modified,
866 unchanged, zero new or removed. All nine metadata records report
`baseline_fallback=true`, baseline commit `62f25809f8521ef07684d2c393438a5ce33f5ee9`,
and merge base `8fcb834d6ff7f8bcfe89fe3e2e4af96fee82343a`.
Re-publication re-resolves baselines and can change the comparison of an old
head even when candidate artifacts stay fixed. This probe intentionally
refreshed that completed PR's review.

## Decision

GitHub's [parallel steps](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsparallel)
share a runner and wait for the entire group. Background steps also have an
implicit wait before post-job cleanup. In this run the host remained occupied
for 19 seconds after the remote test step finished. Putting publication inside
the existing required Bazel job would therefore have delayed that job's
completion on this workload.

This is one bounded concurrency/credential-forwarding probe, not a matched
production CI comparison. It did not measure saved queue time or runner-minutes,
test publication waiting for its own live invocation, or exercise cancellation
and failed-CI handling. Do not move normal publication into the required job
from this result alone. Adoption needs a trusted host control checkout, tested
code confined to RBE, exact run/attempt identity, preserved cancellation and
failure behavior, and a gate design that accepts or avoids the publication tail.
The current Bazel workflow still executes PR-controlled setup on its host;
the prototype's trusted-checkout boundary must precede any credential sharing.

To repeat, dispatch `bench-remote-bazel.yml` on a revision containing the probe
with `mode=visual-colocation` and `visual_ci_run_id=<completed same-repo PR CI>`.
This uses existing publishing credentials and refreshes that review in the
configured bucket; choose a review that may be republished.
