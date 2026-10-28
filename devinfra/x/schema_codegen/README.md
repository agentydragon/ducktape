# Schema conversion spikes

Small source models exercise real schema exporters, production conversion tools, generated
bindings, and consumers. The TypeScript cases use the file-input rules documented in
[SCHEMA_CODEGEN.md](../../js/SCHEMA_CODEGEN.md); generated documents and source files are Bazel
outputs. The `openapi_js_library` and `json_schema_js_library` macros expose declarations through a
`js_library` named by the macro call while preserving the raw `.d.ts` output label. Both default the
raw file to `<name>.d.ts`; the TypeScript fixtures cover omitted outputs and explicit module-path
overrides. Zod generation stays file-only and is compiled by its explicit `ts_library` consumer;
application generators and wire contracts are unchanged.

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
| `infrastructure/pydantic`        | Pydantic → provider-specific JSON Schema → strict-subset and payload validation                                                                                                                |

These are tool-composition and boundary-contract tests. They do not prove that every production
schema is supported, that Python custom validators survive export, or that a remote provider accepts
the published schema. Production migrations need their own producer fixtures and consumer tests.
