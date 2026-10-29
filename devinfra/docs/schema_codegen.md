# Schema mechanisms

Which contract mechanism each boundary in this repository uses, and which to pick for a new one. Macro conventions
for the TypeScript converters: <../js/SCHEMA_CODEGEN.md>. Import rules for generated names:
[STYLE.md § TypeScript](../../STYLE.md#typescript).

## Map

| Boundary                               | Schema language                                                                 | Generator / validator                                                                                                                                                                                                                         | Used by                                                                                                                                        | Pick it when                                                                                                                                                           |
| -------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Service ↔ service                      | Protobuf/gRPC (`.proto`, 24 files, 23 under `agentplane/`)                      | `go_proto_library` (plus `//devinfra/go:grpc_gateway` for `google.api.http` routes and `proto_openapi` for their OpenAPI document), `py_proto_library`, `ts_proto_library` (protobuf-es)                                                      | Agentplane services; `agentplane/app/frontend` consumes the `*_pb` targets with `@bufbuild/protobuf`                                           | Two first-party processes talk, or a stream needs typed envelopes                                                                                                      |
| Browser ↔ HTTP API                     | OpenAPI exported from the production route graph                                | Exporters: `export_schema.py` calling `app.openapi()` per FastAPI app; `agentplane/app/go/schema_export.go` (Huma, `--export-openapi`). Consumers: `openapi_js_library` (types), `openapi_zod_ts_library` (types plus runtime Zod validation) | `openapi_js_library`: agentplane app, aiquota, session_export, plaid spend, haku console, props. `openapi_zod_ts_library`: augur, study casino | A frontend calls a first-party HTTP API                                                                                                                                |
| Python-owned shape, no route           | JSON Schema from Pydantic `model_json_schema` or FastMCP `tools/list`           | `json_schema_js_library`                                                                                                                                                                                                                      | airlock, haku console (tool-call record, MCP tool arguments and results), ssh_mcp_server exec payloads                                         | TS needs a Python model that no HTTP route serves (MCP payloads, stored records)                                                                                       |
| Hand-authored shared vocabulary        | Neutral JSON Schema file                                                        | `//devinfra/js:quicktype_codegen` renders Go and Python types                                                                                                                                                                                 | `agentplane/action_service/sandbox/contract/action.schema.json`                                                                                | A small closed vocabulary that no one language owns                                                                                                                    |
| Vendor-published JSON Schema           | The vendor's JSON Schema                                                        | quicktype via a package-local `generate.mjs` → Go                                                                                                                                                                                             | `agentplane/runner/codex/protocol` (Codex app-server protocol)                                                                                 | An external tool publishes its wire schema and Go must decode it                                                                                                       |
| Go Action inputs                       | Go struct tags                                                                  | `invopop/jsonschema` reflection → `santhosh-tekuri/jsonschema/v6` validation → `encoding/json` decode into the same type (`agentplane/action_service/catalog/input.go`)                                                                       | Action Service built-in Actions                                                                                                                | The advertised schema and the decoder must be one Go type                                                                                                              |
| LLM tool and structured-output schemas | JSON Schema from Pydantic                                                       | `openai_utils.json_schema` (OpenAI strict-mode rewrite); FastMCP reflection for MCP servers; loom inlines `$ref`s itself                                                                                                                      | gmail_archiver, info_gathering evals, loom, haku console MCP exporter                                                                          | A model provider or MCP client consumes the schema                                                                                                                     |
| Config authored in Python, read in Go  | None shared: cdk8s constructs render YAML, viper `mapstructure` structs read it | Hand-synced                                                                                                                                                                                                                                   | `cluster/cdk8s/agentplane/{egress,llm_ingress}.py` and the other Agentplane Go services' `config.go` (10)                                      | Existing services. Pilot: PR #12232 (open) moves the sidecar and LLM ingress pair to protobuf messages with Buf protoyaml and Protovalidate; not policy until it lands |
| CRDs → Python                          | CRD YAML                                                                        | `cdk8s_import` (<../js/cdk8s_import.bzl>, wraps `cdk8s import`) → jsii constructs                                                                                                                                                             | `cluster/cdk8s/providers/*` (upstream operators), `third_party/sshpiper`, `agentplane/crds` (controller-gen CRDs from the Go API types)        | The cluster chart creates custom resources                                                                                                                             |

Rust consumes protobuf only through `rust_prost_library` in `devinfra/js/debundle`, for external OR-Tools messages; no
first-party `.proto` has a Rust consumer.

## Choosing

**Service ↔ service.** Protobuf. Add `google.api.http` annotations and the `grpc_gateway` compiler when the same
service must also serve REST; `proto_openapi` then documents those routes from the proto comments.

**Browser ↔ HTTP API.** Export OpenAPI from the app's real routes and consume it with `openapi_js_library`. Take
`openapi_zod_ts_library` only when the frontend must validate responses at runtime (untrusted or versioned peers).

**Python-owned shape without a route.** Export JSON Schema from the owning Pydantic model, or reflect it through
FastMCP when the MCP `tools/list` response is the contract, then `json_schema_js_library`. Do not add a route just to
put the model in OpenAPI.

**Shared vocabulary.** A neutral JSON Schema file plus quicktype when Go and Python must agree on a closed set and
neither side is the natural owner; otherwise own it in one language and export.

**Go-defined inputs.** The Action input path reflects, validates and decodes from one Go type. Prefer it to a separate
schema file whenever Go is the runtime that both advertises and consumes the contract.

**LLM-facing schemas.** Generate from Pydantic at the call site with `openai_utils.json_schema` for OpenAI strict mode;
MCP servers get their schemas from FastMCP reflection. Hand-written JSON Schema for a tool is a last resort.

**Config across Python and Go.** Today this is hand-synced; keep the two declarations beside each other and change
them in one commit. Wait for the #12232 pilot before adding another mechanism.

**CRDs.** `cdk8s_import` the CRD the cluster applies, including first-party CRDs generated from Go API types.

## Rules

- **Export from the production route graph.** OpenAPI and JSON Schema exporters build the real app or MCP server and
  read its advertised contract; never a schema-only app or a fake-dependency exporter. The Go exporter registers routes
  with placeholder services because Huma reads operation declarations without invoking handlers.
- **Import generated names directly.** See [STYLE.md § TypeScript](../../STYLE.md#typescript).
- **Gotcha:** every TypeScript converter and `quicktype_codegen` runs under `//devinfra/js:schema_remote_fetch_guard`,
  which answers any HTTP `$ref` with a 400 during generation. A schema that resolves a remote reference fails the build;
  declare the referenced file in `schema_files` instead.
