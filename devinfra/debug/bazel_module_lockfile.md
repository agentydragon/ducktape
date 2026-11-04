# Disposable Bazel module-resolution cache

Use `--lockfile_mode=update` and keep the generated `MODULE.bazel.lock` out of Git.
The [CI driver](../ci/bazel_ci.py) restores and atomically saves it under
`${XDG_CACHE_HOME:-~/.cache}/ducktape/bazel/`. BuildBuddy runs `git clean -xfd`
between checkouts, so leaving the generated file only in the repository loses it.
A preexisting workspace lockfile takes precedence. Missing, unreadable or malformed
cached JSON is a cache miss; cache I/O errors are warnings and preserve the CI result.

This is a performance cache, not a maintained dependency lock or a replacement for
source pins. Bazel validates the extension implementation and transitive Starlark,
extension usages and recorded inputs before reuse. `update` recomputes stale
entries. Keep that validation; do not invent a revision-only cache key or edit
individual generated repository specifications.

## Matched APT-resolution experiment, 4 November 2026

Source: `b81957af9c5228332e3808ff26d36186f02ee16c`, Bazel 9.2.0,
`rules_distroless` 0.9.4. One hosted runner uses the source-pinned images,
8 estimated compute units and an 8 GiB JVM heap. Every observation starts a fresh
Bazel server/output base. The observations share a download cache and run in the
order below. RBE, remote caches and BuildBuddy's default snapshot-save policy stay
enabled; no worker image or deployment changes are involved.

Query the generated targets of all twelve APT dependency-set repositories with
`--output=build`; do not analyze/extract their `.deb` payloads. This evaluates the
same shared APT extension without making package availability part of the timing.
The relevant command shape is:

```text
bbr query --lockfile_mode=off --output=build 'set(@trixie_e2e//... ...)'
```

For a matched script, use one runner, independent `--output_base` paths, common
repository-cache settings, and a Bazel `--profile` for every query. Seed the native
lockfile in `update` mode, then use `error` mode to prove strict validated reuse.
All five observations passed:

| Observation                                                                                     | Query elapsed | APT extension |   Fetch/parse |
| ----------------------------------------------------------------------------------------------- | ------------: | ------------: | ------------: |
| [Initial off](https://app.buildbuddy.io/invocation/9f1601cc-d9e8-4794-a540-54ee2bec7955)        |     194.263 s |     181.971 s |     178.213 s |
| [Generate, update](https://app.buildbuddy.io/invocation/184a8514-f291-4d15-bdfc-e01e9a7ab8d8)   |      43.345 s |      33.664 s |      29.833 s |
| [Reuse, error](https://app.buildbuddy.io/invocation/33359190-b9eb-430e-b35a-94320dc306f4)       |       3.185 s | not evaluated | not evaluated |
| [Later off](https://app.buildbuddy.io/invocation/4e00d2e8-15ec-4cf7-86b8-78f64e800c7f)          |      36.506 s |      33.390 s |      29.644 s |
| [Reuse again, error](https://app.buildbuddy.io/invocation/455ac5ff-6a91-4cbe-aeda-02454ca3ff21) |       3.298 s | not evaluated | not evaluated |

The later pair differs by **33.208 seconds**. The initial 190-second gap includes
network/first-use variation and is not a steady-state saving. The first cache
production still costs work. This ordered, single-runner experiment does not
estimate fleet-wide feedback, cache-hit frequency or the behavior of a new worker.
It does not include the driver cache-copy cost in these query durations.

All five queries emit the same 306 Starlark rule statements after parsing away
comments/locations. Their canonical AST SHA256 is
`e0614afe921e66dd0f51b94b1801540172577406ff7f490dade0f4c82acbf6cb`.
The generated `trixie_e2e` hub's rule-marker key, which includes the shared
resolution content, is identical in every observation:
`ce080d1ac421bdea44c82f2b529a2dcbfd2582ff2d73786f8bb0c8b7552a71f3`.
The generated 15,677,854-byte lockfile stays byte-identical across the validated
reuse observations. This establishes target/resolution equivalence for these
inputs; it does not establish cross-machine reproducibility for every extension.

The [evidence export](https://app.buildbuddy.io/invocation/c7fbf1db-e7c0-4704-8d4c-cea4ab1f1645)
contains the five Bazel profiles, generated target listings, hub marker records,
phase results and lockfile. Raw payloads are not committed here.

## Validation and limits

The CI driver's targeted test and library validation passed on RBE:
[80d8173e-bb02-4009-bad7-860e5625b608](https://app.buildbuddy.io/invocation/80d8173e-bb02-4009-bad7-860e5625b608).
That run used the recorded candidate patch over the experiment source, rather
than a later PR commit. Tests cover a cleaned workspace, preexisting workspace
file priority, missing/corrupt cache and preserving failure status when saving fails.

A separate probe exercised the actual driver cache with a real generated lockfile.
It saved outside the checkout, removed the workspace copy with `git clean -xfd`,
restored it and ran a fresh-server query in production `update` mode. The
[restored query passed](https://app.buildbuddy.io/invocation/0e4ed347-e5ea-4a01-b316-e177648b906a),
with the same 306 statements/AST digest above and no APT extension evaluation.
Adding `coreutils` to the direct `trixie_e2e` package requests then caused strict
[`error` mode to reject changed usages](https://app.buildbuddy.io/invocation/841e3e0a-165d-4f99-a6e7-d1b8b392ed17).
Production [`update` mode recomputed successfully](https://app.buildbuddy.io/invocation/9c7e926a-c943-48c8-b3b6-d843058369a5)
and emitted the new `trixie_e2e/coreutils/amd64` targets. Its APT evaluation took
25.800 seconds. This tests both reuse and native stale-input invalidation.

The [probe export](https://app.buildbuddy.io/invocation/544ae595-f694-4f46-b59b-e98c59e605df)
contains all three profiles, target listings, stderr, results and a Python cProfile.
The cache helper consumed 0.525 seconds cumulatively across four context uses
(seed save and three query contexts), including JSON validation and file copies.
The restored query itself took 11.616 seconds on this separate runner; it is
correctness evidence, not another observation in the matched timing table.

The first attempted full `//...` analysis
[failed](https://app.buildbuddy.io/invocation/543642c1-1443-4477-8561-656eb80dfd33)
after 517.147 seconds on a Debian snapshot `.deb` HTTP 504. Its APT extension
completed in 325.297 seconds, including 321.345 seconds fetch/parse, but that
failed run is not a matched full-CI baseline. Resolution caching does not cache
missing package payloads or eliminate those network failures.

`mod show_repo` was also unsuitable for isolating the experiment: it eagerly
evaluated unrelated extensions and failed on existing transitive crate-universe
and Envoy/Google APIs imports. The successful comparison uses ordinary query
loading instead. No coverage or test selection policy changes accompany this cache.

## Earlier decision

On 19 October, source `5d109dcd7`, a full `//...` analysis with fresh output bases
and an 8-unit runner measured `off` 121 s, `update` 116 s and strict reuse 105 s.
The 13.9 MB maintained lockfile, mostly APT metadata, was left disabled because
its roughly 16-second gain did not justify regeneration and parallel PR conflicts.
Those observations remain historical, not a before/after comparison with November.

The current proposal changes that maintenance tradeoff: the file is generated,
ignored and local to the runner, with Bazel doing input validation and refresh.
Cache misses and input changes still pay resolution cost. Measure source-matched
required-gate feedback after rollout before claiming an end-to-end speedup.

Primary references: [Bazel lockfile semantics](https://bazel.build/external/lockfile),
[Bazel 9.2 validation](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/bazel/bzlmod/SingleExtensionEvalFunction.java),
[BuildBuddy checkout cleanup](https://github.com/buildbuddy-io/buildbuddy/blob/d84e5dfd97439eeefbbf247681de47f3960adc11/enterprise/server/cmd/ci_runner/main.go).
