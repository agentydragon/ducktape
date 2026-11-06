# Proto-first policy CRD experiment

This is an isolated experiment, not a replacement for the production PolicySet CRD or Policy Service.
It uses the separate `protofirst.agentplane.allegedly.works` API group. `policy.proto` defines a
PolicySet spec, generates an Istio `protoc-gen-crd` CRD, and provides a local-only gRPC probe. `codec`
wraps the generated protobuf spec in a Kubernetes object and uses ProtoJSON while Kubernetes metadata
stays native.

Unlike the existing CRD, the Action and entity maps use protobuf message values, for example
`actions: {github: {names: [get_file_contents]}}`. The generator's list-element annotations enforce
nonempty names, `google.api.field_behavior = REQUIRED` marks required fields, and `google.protobuf.Struct`
retains the structure of Action argument schemas. `Struct` represents JSON numbers as doubles and can
change large integers: the test demonstrates `9007199254740993` becoming `9007199254740992`. Proto field comments produce CRD descriptions, but this
version of the generator includes only the first sentence.

`//agentplane/policy/protofirst/codec:codec_test` installs the generated CRD in an isolated API server.
It checks descriptions, required fields, CEL validation, map/list semantics, typed reads, arbitrary
JSON Schema round-tripping, Kubernetes watch events and a local gRPC read. Representative cases cover
egress, Action, Kubernetes role binding and session inbox rules. A separate `:listvalue_probe` verifies
that the upstream generator rejects the original map-of-`ListValue` shape as non-structural.

This is not proof of parity with the production CRD. In particular, it does not cover every rule,
validation edge case, policy manifest, client or protobuf presence case; PolicyBinding status and
server-side-apply ownership; production service watches, authorization or integration; or migration of
existing resources. The Action and entity shape changes require a coordinated migration. The explicit
Bazel CEL repository mapping in `MODULE.bazel` is experimental and needs a maintainable dependency
arrangement before adoption.
