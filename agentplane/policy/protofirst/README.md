# Proto-first policy CRD experiment

This is an isolated experiment, not a replacement for the production PolicySet CRD or Policy Service.
It uses the separate `protofirst.agentplane.allegedly.works` API group and a reduced egress-only CRD.
`policy.proto` defines the protobuf types and an experimental local-only gRPC probe. `crd.bzl` invokes
Istio's `protoc-gen-crd`; `codec` wraps the generated PolicySet spec in a Kubernetes object and uses
ProtoJSON for the spec while Kubernetes metadata stays native.

`//agentplane/policy/protofirst/codec:codec_test` installs the generated CRD in an isolated API server,
checks egress admission, creates a manifest-shaped resource, reads it via the protobuf-backed Go wrapper,
and retrieves the same spec over a local gRPC server. It separately checks JSON and deepcopy behavior
for an existing Action policy shape. It also depends on `:listvalue_probe`, which verifies that the
unmodified upstream generator currently rejects that Action map shape as non-structural. This expected
failure is a limitation to solve, not an assertion that every PolicySet has been migrated.

Not covered: the full PolicySet rule and CEL schema, PolicyBinding status and server-side-apply field
ownership, watches, production authentication, schema/version migration, all live policy manifests,
or integration with the existing PolicyService. The Action policy shape is tested for JSON conversion
only; the generated egress CRD cannot admit it. The explicit Bazel CEL repository mapping in
`MODULE.bazel` is experimental and needs a maintainable dependency arrangement before any adoption.
