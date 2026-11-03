# Proto-first policy CRD experiment

The production policy API has an existing map of ActionGroup names to arrays of Action names, e.g.
`actions: {github: [get_file_contents]}`. In proto3, `map<string, google.protobuf.ListValue>` can
serialize that shape as ProtoJSON, but it does not constrain the list elements to strings in the
protobuf type itself.

Istio `protoc-gen-crd` at `v0.0.0-20260928195158-7fc2b03dd59b` has a built-in schema for
`google.protobuf.ListValue` that supplies object items without an array `type`. When that ListValue is
a map value, it rejects the generated CRD as non-structural: `additionalProperties.type` is missing.
The action is reproduced by `//agentplane/policy/protofirst:listvalue_probe`, which expects that
specific upstream failure and fails if the generator starts succeeding or fails differently. The
original failing build was [BuildBuddy 2bb365ed](https://app.buildbuddy.io/invocation/2bb365ed-c3d1-4293-bb06-a8667eff7e35).

A narrow egress-only proto can generate a structural CRD. The protobuf-backed wrapper requires an
explicit ProtoJSON codec and a protobuf-aware deep copy. A real API server test installs the generated
CRD in an isolated API group, checks CEL admission, reads a manifest-shaped resource into the typed
wrapper, and forwards the spec over a local gRPC endpoint. This is evidence for that reduced shape,
not proof of equivalence with the deployed CRD. See the prototype README for coverage and exclusions.

An adoption decision still needs a representation or generator extension that retains the existing
string-array JSON, Kubernetes list and map validation, CEL rules, status field ownership, and all
existing policy manifests. Replacing the map's arrays with protobuf wrapper objects would change the
stored/user-authored Kubernetes shape and needs a deliberate migration; it is not done here.
