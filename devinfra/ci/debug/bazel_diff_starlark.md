# bazel-diff Starlark selection gaps

`bazel_ci.py` enables `-co=--proto:include_synthetic_attribute_hash` for both
base and candidate hash generation. Keep the graph-wide `.bzl` fallback:
the native attribute fingerprint repairs the selector miss below, but does not
cover every package or global input. Ordinary BUILD edits do use bazel-diff,
so the selector repair matters independently of narrowing the `.bzl` fallback.

## Verified behavior

Probes on 4 November 2026 used Bazel 9.2.0 and the installed bazel-diff 47.0.0,
from Ducktape `52ff7de63dbcdfb082b7f84f1a7f7419bdf7dd8e` through hosted `bbr`.
Each counterexample built the actual output before and after the edit and compared
target selection with and without the native fingerprint.

- Ordinary macros, transitive macro/rule helpers, and constants changing rule
  attributes selected their expected reverse dependents; an unchanged control
  selected nothing. [Probe evidence](https://app.buildbuddy.io/invocation/9c6cbf52-52f1-4319-93ae-c4253d92ac51).
- A scalar `select()` changed its built output from `LEFT` to `RIGHT`, but default
  hashing missed the rule and dependent. The native fingerprint selected both.
  This reproduced with a [loaded helper](https://app.buildbuddy.io/invocation/c91d3044-31ca-4a32-8f40-547e9d0b0163)
  and [directly in BUILD](https://app.buildbuddy.io/invocation/53447e1e-263f-46a7-aa62-2ba57ce1a074).
- A `.bzl` constant passed to `package(features=...)` changed a rule's output
  through `ctx.features`, but its hash and dependent's hash stayed identical.
  [Default hashing evidence](https://app.buildbuddy.io/invocation/87d22192-574f-44e5-a8ec-94fd61ade1ef).
  [The native-fingerprint probe](https://app.buildbuddy.io/invocation/aecb3532-caea-47e6-a40f-4eef270f6547)
  still missed the rule; it intentionally failed its selection assertion.

## Why the gaps remain

Default query proto flattens configurable attributes. Bazel's synthetic attribute
hash preserves their raw selector representation, avoiding a separate Starlark
parser. It is an attribute fingerprint, not a complete package-state fingerprint.

Bazel emits package features in `Build.Rule.default_setting`, which
[v47's rule digest](https://github.com/Tinder/bazel-diff/blob/57ca4c9bc2009f452dbdc34ce758415da494d73e/cli/src/main/kotlin/com/bazel_diff/bazel/BazelRule.kt)
does not hash. [Upstream issue #441](https://github.com/Tinder/bazel-diff/issues/441)
discusses loaded-package state and configurable-attribute gaps; it does not
establish that package features are covered. A source review of v74.0.2 found
that its Rust implementation also clears `default_setting` in `compact_target`;
that release was not behaviorally tested here. An upgrade alone is not a verified
repair.

Before narrowing the `.bzl` fallback, add upstream coverage for the demonstrated
package-default omission and rerun these behavioral cases with the installed tool.
Audit globally applied aspects, module/repository extensions and selected
toolchains separately: the probes do not establish coverage of those inputs.
Use Bazel's fingerprints and upstream hashing fixes rather than a custom load
scanner or a second dependency graph. These observations establish selection
behavior, not a CI latency saving.
