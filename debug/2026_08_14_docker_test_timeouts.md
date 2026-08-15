# Docker tests time out under a full `//...` run, and it is a hang

Five targets turned devel and a PR red on 2026-08-14 with no code near them changed. The first
reading was "they sit close to their timeout and contention tips them over", which would make the
fix a `size` bump. **That reading is wrong, and the bump would have hidden the real thing.**

## What failed

devel `19b7f5ce9` — four TIMEOUTs and one flake:

| Target                                                   | Under `//...` | Alone               |
| -------------------------------------------------------- | ------------- | ------------------- |
| `//props/agents/grader:test_matchable_occurrences`       | TIMEOUT 60.3s | —                   |
| `//props/backend/routes:test_runs`                       | TIMEOUT 60.2s | 47.9s               |
| `//props/db/sync:test_model_metadata_sync`               | TIMEOUT 61.7s | 43.3s               |
| `//props/specimens/ducktape/2025-11-22-01:test_specimen` | TIMEOUT 60.2s | —                   |
| `//x/codex_execpolicy_audit:rules_test`                  | FAILED 0.5s   | PASSED 0.9s (flake) |

PR #4014 — one more, and the one that gives the game away:

| Target                                  | Under `//...`  | Alone |
| --------------------------------------- | -------------- | ----- |
| `//haku/console:test_operator_identity` | TIMEOUT 308.8s | 27.7s |

## Why it is not slowness

27.7s against a 300s ceiling is not a test drifting up against its limit. It is **11x**, and the
test logs say where the time goes — which is nowhere:

- `test_model_metadata_sync` collected its one item, printed the test's name, and then emitted
  nothing for the remaining ~60s. The test **started** and stopped making progress.
- `test_operator_identity` never reached collection. Its whole log is the `fastapi.testclient`
  import warning, then 308 seconds of silence.

A test starved of CPU still makes progress and still logs. One that produces nothing at all is
blocked, and both of these block before or during fixture setup.

## The common factor

**Every one of the six is `requires_docker = True`.** 143 targets across the tree declare it.

The container fixture is not the problem, and that is worth saying because it is the first place
you would look: `postgres_container` is already **session-scoped**, and `db_url` already creates a
**database per test** inside it. Within one target that is exactly the right shape.

The unit that is not shared is the **Bazel test target**. Each is its own process with its own
pytest session, so each starts its own Postgres, and a full `//...` runs as many concurrently as
there are job slots against one daemon per RBE worker.

## The mechanism: a thundering herd on the image load

`start_postgres_container` checks whether `postgres:18` and `testcontainers/ryuk:0.8.1` are present
and, on a miss, pushes the OCI layout into the daemon with crane. The module's own comment prices
that at ~30s of layers.

**That check was a race with no lock.** On a cold worker every concurrent starter runs
`docker image inspect`, every one is told the image is absent, and every one then pushes the same
layers into the same storage driver at the same time. One ~30s load becomes dozens of them.

It accounts for every observation:

- **The silence** — it happens in session fixture setup, before pytest emits a line.
- **The spread** — whichever targets start when the daemon is coldest wait longest, which is why a
  28s test hung for five minutes while ~45s tests hung just short of one.
- **`test_operator_identity` never reaching collection** — it was still in the herd.

## What was done

- `load_oci_image` takes a **per-tag `flock`** and re-checks presence **inside** it, so the first
  waiter to acquire the lock finds the image already loaded and skips. One load per tag per machine
  instead of one per process. `flock` releases on fd close, so a loader that dies mid-push does not
  strand the rest.
- **The wedge is loud.** Waiting on the lock, loading, and starting the container each log with
  elapsed time, so a future hang names the step it is in instead of producing an empty log.

## A wrong turn worth recording

The first attempt at "is it already loaded" compared the daemon's `.Id` for the tag against the
**config digest read from the OCI layout**, on the reasoning that an image's id _is_ its config
digest and so no state need be written down. It is not, here: Docker's classic image store rewrites
an OCI config on load, so `.Id` is the digest of the rewritten one.

The failure mode is what makes this worth a note. A comparison that never matches does not fail
visibly — it silently reloads every image on every target, serialised behind the new lock, which is
**slower than doing nothing**. It went out and two more specimen targets and an editor-agent test
timed out on the next run.

So the daemon's id is now recorded rather than predicted, and only ever compared against itself:
the marker holds `<layout digest> <daemon id>`, and a skip needs both to still hold — the layout so
a pin bump or a rebuild reloads, the daemon id so a prune or an out-of-band retag does too.

## Still open

- **The daemon's own container start is unbounded.** The lock removes the herd on the _load_; N
  concurrent `container.start()` calls are still N. If timeouts persist, that is the next thing to
  measure, and the new timings point straight at it.
- **Sharing one container across targets** would remove the contention entirely, but is a different
  kind of change: it needs a daemon-side singleton outliving any one test process, which Ryuk is
  currently configured to reap.

## What not to do

Bump `size`. It converts "red at 60s" into "red at 300s" — the same failure, five minutes later,
having spent five minutes of RBE per occurrence to reach the same place, with the evidence buried
one tier deeper. This is exactly the case AGENTS.md's "test timeouts mean hangs, not slowness — do
NOT bump `size`/`timeout`" is written for; the measurements above are what that rule asks for before
reaching for the knob, and they say hang.

`size` was therefore not bumped anywhere. If a target still times out after this, that is evidence
about the daemon rather than an argument for a bigger number.

## If it happens again

The timings added above are the first thing to read: they say whether the wait was on the lock, the
load, or the daemon's own container start. Only the third is still unbounded, and bounding it would
mean a `resource_set` or exec property on the `requires_docker` branch of `py_test` — the one place
that already knows which tests those are. Worth choosing against a measurement rather than in
advance, since a port range exhausted and storage-driver contention look the same from here and are
not distinguished by anything observed so far.

## Unrelated, seen on the way

`//x/codex_execpolicy_audit:rules_test` is a genuine flake — 0.5s failure under `//...`, 0.9s pass
on re-run, no Docker involved. Not part of this.
