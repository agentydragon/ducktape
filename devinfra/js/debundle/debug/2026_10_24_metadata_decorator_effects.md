# Metadata decorator timing and class callback reachability

A target-local mutation contract does not establish callback timing. A bundled
member-decorator helper retrieves a descriptor, calls supplied decorators, then
restores the descriptor. An opaque decorator can call the method immediately;
an audited metadata-only decorator stores annotations without running it.
Promoting every target method at the latter call site invents initialization
reads and prevents otherwise valid module splits.

The first neutral reduction exposed the opposite problem: class/prototype
surfaces were dropped by abstract callback discovery. With the existing
`typescript_decorate_helper` contract, an imported decorator that calls a method
before its lexical dependency initializes incorrectly accepted the split.
The old-schema safety regression fails in
`87677198-c4d3-4693-9af3-76f09ef9392b`; the six focused class cases and existing
imported callback cases pass after class-surface preservation in
`87e43186-eae9-494b-a290-fe7750cc6419` ([#11879](https://github.com/agentydragon/ducktape/pull/11879)).
A follow-up counterexample showed that promoting class-method rebinds without
updating prefix values still accepted a later imported callback invocation before
initialization. It fails in `26fc5512-69f6-478e-94cd-4073eba138eb`; all seven class
cases pass after prefix propagation in `97f5e06a-8292-4c75-9e33-3e1a164e7c39`.
The safety repair retains ordinary opaque-call behavior and adds no trust assertion.

The separate metadata-only capability adds an application-wide author assertion
with a checked whole-statement member-decoration shape. Every helper branch and
supplied decorator, including imports, must be audited. It does not derive timing
from purity or from the weaker mutation annotation. Static helper/local-decorator
binding dependencies, eager arguments and class-local mutation constraints remain.
The maintained contract is in SPEC.md; details of the source-only versus trusted
proof boundary belong there rather than in consumer-specific heuristics.

The neutral fixture compares original/emitted execution and observable metadata
for both function declarations and variable-arrow helpers, with both admitted
flags. Rejection controls cover the weaker contract, actual method invocation,
mutations split from their class, unsupported argument shapes, local decorator
binding reads, and helper reassignment. The variable-arrow form matters because
its runtime abstract value is a closure even when it captures no local state.

The metadata library and fixture compiled with Bazel/Rustfmt/Clippy in
`4449ef48-b27b-46b0-b66a-489f9e31086f` before integration with the class repair.
This build is not runtime validation of the combined model. Standalone local
getter reads and closed host/export reentry proofs remain separate capabilities.

The first integrated metadata checks passed in
`4d46010f-9556-4ab7-a8a3-5d54d893d0bf`: seven metadata cases and the analysis
suite. The final combined validation includes the prefix-rebind follow-up too.
