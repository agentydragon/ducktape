# Code reduction decisions

Record operator decisions here when they change whether an audit finding is ready to pursue.
Use the project's active plan for implementation details and test evidence.

## TypeScript build refactor

**Compare alternatives before choosing a direction.** Repetitive BUILD declarations are one
part of the TypeScript build problem. Changes to target granularity, dependency declarations,
generated inputs, lint, and tests can reinforce or work against each other.

Spike alternatives on the same representative slice. Compare BUILD generation with the
[Aspect Gazelle JS extension](https://github.com/aspect-build/aspect-gazelle/tree/main/language/js),
simpler rules or macros on the existing graph, and coarser targets where a package or feature
boundary fits. Keep the current setup as the baseline. Measure manual edits and net maintained
code, including generation and overrides. Compare affected-target selection and test coverage
for ordinary edits, and use a slice with generated inputs, assets and tests.

Review the results together before picking a tooling or layout change. Treat BUILD generation
as a candidate experiment in trimcode until that review.

## Authored Threads before History teardown

**Prove the opt-in mode in use before starting the teardown.** Build authored Threads additively
behind an off-by-default setting, with a UI for the operator to use. Test the new path and the
existing path with the setting off. The operator then uses authored Threads for real work and
decides whether the mode works well enough to proceed.

Keep the History fold, projections and sync serving during that trial. Start their teardown
or simplification only after the operator accepts the new mode. Then verify the remaining
History consumers, including raw diagnostic reads, command reconciliation and retained data,
before removing their dependencies. Treat that cleanup as gated migration work in trimcode.

## Persistent Agentplane test cluster

**Keep the prototype under `x/`.** The
[shared-cluster experiment](../agentplane/system_tests/x/shared_cluster/README.md) remains
available while the end-to-end test architecture is open. Per-action K3s is the current setup.

Explore moving behavior tests off Kubernetes where they can still exercise the relevant contracts.
For coverage that needs Kubernetes, explore sharing a test cluster with persistent infrastructure
such as KubeVirt and cert-manager. The future suite may use both. Revisit prototype retirement after
the test architecture decision.

## Shared Go and Python settings

**Protobuf is the leading schema candidate.** Generate bindings for Python deployment renderers
and Go services from the same definitions. Evaluate this against the current typed YAML setup
before choosing a framework or planning service migrations.

- **Settings file format.** Compare protobuf-aware YAML, textproto, and binary protobuf. Check
  field presence and defaults, unknown fields, deployment diffs, and startup diagnostics.
- **Validation.** Go must reject invalid settings at startup. It is acceptable to start with
  Go-only validation. Python can render a well-formed message that the runtime rejects on
  semantic grounds. Consider shared schema rules, such as Protovalidate, if catching errors
  during synthesis is worth the integration cost. Keep runtime Secret inputs explicit.

[The sidecar and LLM ingress spike](https://github.com/agentydragon/ducktape/pull/12232) tried
generated bindings, Buf protoyaml, and Protovalidate. Use it as evidence for that combination.
