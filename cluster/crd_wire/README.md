# CRD-derived Python wire types

`bbr test //cluster/crd_wire:test_wire` generates external-key `TypedDict` types from the
installed Flux `ArtifactGenerator` CRD and the Go-derived Agentplane `SandboxTemplate` CRD.
The test loads both committed `ArtifactGenerator` resources and the Pod, VM and Firecracker
`SandboxTemplate` resources from staging. The Bazel action consumes the same schema files
as the deployed CRDs. It writes Python and the selected version's JSON Schema in `bazel-bin`.

`extract_crd_bin --openapi-schema` selects the registered version, preserves its field
schemas, and derives the `apiVersion` and `kind` constants from the CRD. Pinned
[datamodel-code-generator 0.99.0](https://github.com/datamodel-code-generator/datamodel-code-generator/tree/0.99.0)
produces static wire types. A JSON Schema check before the cast applies required fields,
enum values, bounds and patterns to the original dictionary without converting it.
For example, `copy[0]["from"]` uses the exact required wire key, and the schema rejects
`firecracker.memoryMiB = 127`. Unknown claim and Pod fields survive intact.

## Alternatives tested

- cdk8s already generates `ArtifactGeneratorSpecArtifactsCopy` for synthesis. Its Python
  constructor takes `from_` and rejects a dictionary containing `from`. Pydantic
  `TypeAdapter` cannot generate a schema for that jsii class. With arbitrary types enabled
  it accepts an existing instance and still rejects the input dictionary.
- `TypeAdapter` on the generated `TypedDict` preserves both committed ArtifactGenerators.
  It accepts an out-of-range Firecracker memory value and drops preserved fields of
  `workspaceVolume.claim` beyond the declared `volumeMode`. Static dictionary access needs
  the generated type. Raw dictionary fidelity and structural validation need the schema.
- The same generator's `pydantic_v2.BaseModel` output validated all five committed objects
  and rejected the low memory value. `model_dump(mode="json", by_alias=True, exclude_unset=True)`
  recovered the original dictionaries, including both opaque and mixed claims. Python
  attributes are model names such as `from_`, and validation constructs a new object.
- [Piceli 0.61.0](https://docs.pynenc.org/projects/piceli/en/stable/crds.html) took each
  CRD directly, round-tripped all five `spec` fragments with Pydantic, and rejected the
  low memory value. Its generated files cover `spec`, while `ArtifactGenerator` readers
  can also use `status` and metadata.

## Boundary

`Draft7Validator` covers the ordinary schema keywords exercised by these inputs. CRD
schemas also use OpenAPI and Kubernetes extensions. An integer-or-string field, Kubernetes
nullability, constrained CEL comprehensions, list map uniqueness, defaulting and server-side
field ownership require their own handling before widening this prototype to other CRDs.
A SandboxTemplate specifying both `pod` and `vm` passes the prototype's structural check
and fails CEL in the [existing admission test](../../agentplane/crds/test_sandbox_admission.py). Opaque objects such as Pod templates carry a dictionary type;
`workspaceVolume.claim`'s typed surface exposes the known key while retaining the raw fields
for callers that operate on the original dictionary.

The Pod, VM and Firecracker cdk8s builders use the generated `StateVolume` to share
the same volume shape as the CRD, and Firecracker checks its backend spec against the
generated `Firecracker` type. Workspace claim fields still pass through as a dictionary
so fields Kubernetes preserves can be set by the builders.

Cluster validation continues to accept partial ArtifactGenerator input. Its source index
can use an artifact name even when the document has no copy directives or source list.
