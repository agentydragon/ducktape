# bazel-diff package-feature hashing patch

[package-default-features.patch](package-default-features.patch) applies to
[bazel-diff v74.0.2, `f83686c9196aff4d23a9d24d1e529193c37e9687`](https://github.com/Tinder/bazel-diff/tree/f83686c9196aff4d23a9d24d1e529193c37e9687).
It preserves and hashes package-level features exposed by Bazel as
`Rule.default_setting`, including the streaming prehash path. It also invalidates
persisted hashes and partial recipes produced under the previous hash semantics.
The patch includes regression tests for feature membership, order, framing and
reverse-dependency propagation.

Patch SHA-256: `6adc226c6861b9ae53f468e6f148ceb0fa0d724c230100de5aed97bf66b779a4`.
The prepared upstream commit is `29e236f62b85cc22d1aa38704307ba1d893709c3`;
it remains local and has not been submitted upstream.

Apply the patch in a clean checkout of the pinned upstream revision with
`git apply --check <patch-path>` followed by `git apply <patch-path>`. The patched
`//src:rust_tests`, `//src:cli_tests` and `//src:bazel-diff` targets passed on RBE.
The [CI investigation](../../devinfra/ci/debug/bazel_diff_starlark.md#prepared-package-feature-repair)
records the actual Bazel behavior probes and validation links.

## Integration boundary

This directory retains the tested patch for review. Ducktape's
[Nix package](../../nix/packages/bazel-diff.nix) still runs the v47.0.0 JAR; the
Rust patch is not applied by that derivation. Adopting it requires packaging the
patched Rust release or separately porting and validating the repair for v47.
The Rust CLI also needs `-co` and its value as separate arguments, or the canonical
`--bazelCommandOptions` option; it does not accept the v47 spelling `-co=<value>`.

Keep the broad `.bzl` fallback until the installed tool passes the behavior probes
and the remaining package/global-input gaps have been audited. This patch alone
establishes no CI latency saving.
