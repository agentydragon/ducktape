# Proto-first policy CRD experiment

The production policy API maps ActionGroup names to arrays, e.g. `actions: {github:
[get_file_contents]}`. Proto3 cannot directly declare a map whose value is `repeated string`.
`map<string, google.protobuf.ListValue>` produces that JSON shape, but does not constrain its
contents, and Istio `protoc-gen-crd` at `v0.0.0-20260928195158-7fc2b03dd59b` rejects its generated
schema as non-structural because the array type is missing. `//agentplane/policy/protofirst:listvalue_probe`
reproduces the expected failure; the original failing build was [BuildBuddy
2bb365ed](https://app.buildbuddy.io/invocation/2bb365ed-c3d1-4293-bb06-a8667eff7e35).

A map of typed protobuf messages instead generates a usable CRD: `actions: {github: {names:
[get_file_contents]}}`. This changes the existing Kubernetes shape. The generator accepts the
`+protoc-gen-crd:list-value-validation:MinLength=1` marker for array elements; the analogous
`+kubebuilder:validation:items:MinLength=1` was applied to the array itself and failed generation.
Required fields need `(google.api.field_behavior) = REQUIRED`. Kubernetes set and keyed-list markers,
CEL checks, and `google.protobuf.Struct` for nested JSON Schema work in the covered cases. However,
`Struct` stores numbers as double-precision floats: the precision probe confirms that the bound
`9007199254740993` round-trips as `9007199254740992`. This needs a representation or explicit
constraint before adoption; no arbitrary-JSON fidelity is claimed.

Proto comments become CRD field descriptions, but the generator deliberately takes only the first
sentence; descriptions without a period may be omitted. The experiment checks generated descriptions,
including nested map values.

The test API server installs an isolated CRD covering the PolicySet rule fields, checks representative
valid and invalid rules, preserves nested arbitrary JSON Schema, and reads/watches Kubernetes objects
through a protobuf-aware JSON wrapper. A local gRPC probe returns the same spec. These are representative
checks, not a proof of full production admission, watch, or server-side-apply equivalence. The current
Bazel CEL repository mapping is also an experimental shortcut; see the prototype README for exclusions.
