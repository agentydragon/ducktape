# Schema conversion spikes

Small source models exercise real schema exporters, production conversion tools, generated
bindings, and consumers. The [repository pipeline map](../../docs/schema_codegen.md) describes
the production paths. The TypeScript cases use the file-input rules documented in
[SCHEMA_CODEGEN.md](../../js/SCHEMA_CODEGEN.md); generated documents and source files are Bazel
outputs. The `openapi_js_library` and `json_schema_js_library` macros expose declarations through a
`js_library` named by the macro call while preserving the raw `.d.ts` output label. Both default the
raw file to `<name>.d.ts`. `json_schema_js_library` also exposes the original JSON and declared
reference files at their producer paths, so consumers import JSON and types through one dependency.
The TypeScript fixtures cover omitted outputs and explicit module-path overrides.
`openapi_zod_ts_library` returns a compiled `ts_library` and preserves the generated source label,
defaulting to `<name>.ts`.

Agentplane's Action path is also in the repository map: `catalog.Input[T]` reflects and validates Go
inputs at runtime, while the Sandbox `action.schema.json` generates Go and Python enums through pinned
`quicktype-core`. The production roster check is
[`//agentplane/action_service/sandbox/contract:contract_test`](../../../agentplane/action_service/sandbox/contract/BUILD.bazel).

Run all cases, including lint and language checks on the explicitly selected library targets:

```bash
bbr test //devinfra/x/schema_codegen/... //loom/gym:test_schema_codegen --test_output=errors
```

The runtime suites can also be selected together with
`bbr test //devinfra/x/schema_codegen:tests --test_output=errors`. Selecting only the suite does
not independently select every Python library for its mypy/ruff aspects.

| Package                          | Cases                                                                                                                                                                                          |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `typescript`                     | Pydantic/FastAPI export; production OpenAPI/JSON Schema converters; cross-package schema/config references; denied remote fetches; naming limits; typed vocabulary; Zod → JSON Schema → Python |
| `protobuf`                       | Imported proto graph → JavaScript/declarations, Python, Go, Rust, and Swagger; native gRPC/gateway; typed consumers and cross-language ProtoJSON fixtures                                      |
| `infrastructure/quicktype`       | JSON Schema → generated Go models → compiled runtime consumer                                                                                                                                  |
| `infrastructure/kubernetes`      | Go API types → CRD → Python/jsii construct → synthesis; generated Go API clients                                                                                                               |
| `infrastructure/discovery`       | Google Discovery fixture → actual MCP adapter → advertised JSON Schema                                                                                                                         |
| `infrastructure/grocy_openapi`   | OpenAPI → real Grocy/FastMCP tool adapter → advertised schemas and mocked HTTP invocation                                                                                                      |
| `//loom/gym:test_schema_codegen` | Private Loom adapter → flattened nested provider JSON Schema; test stays in owning package                                                                                                     |
| `infrastructure/pydantic`        | Pydantic → shared OpenAI strict-schema helper → strict-subset and payload validation                                                                                                           |

These are tool-composition and boundary-contract tests. They do not prove that every production
schema is supported, that Python custom validators survive export, or that a remote provider accepts
the published schema. Production migrations need their own producer fixtures and consumer tests.
