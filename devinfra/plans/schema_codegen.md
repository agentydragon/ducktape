# Schema generation: inventory, boundaries, and migration record

This design separates schema export, format conversion, and language compilation. Its fixture
spike lives in [`devinfra/x/schema_codegen`](../x/schema_codegen/), where small source models exercise
real generators and consumers.

The inventory below and the whole-repository “before” map record the production state at immutable
design-base commit [`6eb2ffc2131fff18eaedcbdcf12d23aa93187edf`](https://github.com/agentydragon/ducktape/commit/6eb2ffc2131fff18eaedcbdcf12d23aa93187edf).
Links to the former macro files point to that baseline because those files are removed by the
implementation. The “after” map describes the combined target state, including the direct named
imports migrated in [PR #12418](https://github.com/agentydragon/ducktape/pull/12418). The file-input
converter implementation is stacked in [#12477](https://github.com/agentydragon/ducktape/pull/12477)
→ [#12484](https://github.com/agentydragon/ducktape/pull/12484) →
[#12489](https://github.com/agentydragon/ducktape/pull/12489) →
[#12499](https://github.com/agentydragon/ducktape/pull/12499); this spike follows that production
stack.

## Baseline inputs, outputs, and owners

The inventory covers schema and typed-binding generation, including runtime schema adapters.
Database migrations, ordinary serialization, UI rendering, and bundling are outside this scope.

| Source and owner                                                                                                                                                                                                                                                                                                               | Export/intermediate                                                                                         | Conversion and output                                                                                                                                                                                                               | Consumer                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| FastAPI/Pydantic: [Agentplane](../../agentplane/app/export_schema.py), [Props](../../props/backend/export_schema.py), [Haku](../../haku/console/export_schema.py), [AIQuota](../../aiquota/export_schema.py), [Spend](../../finance/plaid/spend/export_schema.py), [session export](../claude/session_export/export_schema.py) | OpenAPI JSON                                                                                                | [baseline `js_openapi_schema`](https://github.com/agentydragon/ducktape/blob/6eb2ffc2131fff18eaedcbdcf12d23aa93187edf/devinfra/js/openapi.bzl), `openapi-typescript` → `.d.ts` containing `paths`, operations, and component models | Frontend component types; Agentplane, Props, and Haku also use operation types with `openapi-fetch` |
| FastAPI/Pydantic: [Augur](../../finance/augur/api/export_schema.py), [Study Casino](../../study_casino/export_schema.py)                                                                                                                                                                                                       | OpenAPI JSON, augmented with existing models for undocumented CSV requests or WebSocket frames              | [baseline `js_openapi_zod`](https://github.com/agentydragon/ducktape/blob/6eb2ffc2131fff18eaedcbdcf12d23aa93187edf/devinfra/js/openapi.bzl), Hey API Zod plugin → `.ts` for Augur or `.mjs` for Study Casino                        | Typed request/response validation; JavaScript fetch/WebSocket validation                            |
| [Pinned Grocy OpenAPI](../../grocy_mcp/BUILD.bazel), [request-scoped adapter](../../mcp_infra/request_scoped_openapi.py)                                                                                                                                                                                                       | Upstream OpenAPI 3.1 JSON → [Grocy-owned repairs](../../grocy_mcp/fix_openapi_spec.py)                      | FastMCP `OpenAPIProvider` → advertised tool input/output JSON Schema and executable HTTP tools                                                                                                                                      | [Grocy MCP](../../grocy_mcp/server.py); Grocy deliberately drops unreliable upstream output schemas |
| [SSH FastMCP tool signatures](https://github.com/agentydragon/ducktape/blob/6eb2ffc2131fff18eaedcbdcf12d23aa93187edf/ssh_mcp_server/export_frontend_schema.py)                                                                                                                                                                 | Tool input/output JSON Schemas are wrapped in synthetic OpenAPI 3.1 with an invented `/exec` HTTP operation | `js_openapi_zod` → typed Zod module                                                                                                                                                                                                 | Agentplane SSH action renderers                                                                     |
| [Airlock models](../../airlock/export_schema.py), [Haku ToolCallRecord](../../haku/console/export_tool_call_schema.py)                                                                                                                                                                                                         | Pydantic serialization-mode JSON Schema                                                                     | [baseline `js_json_schema`](https://github.com/agentydragon/ducktape/blob/6eb2ffc2131fff18eaedcbdcf12d23aa93187edf/devinfra/js/json_schema.bzl), `json-schema-to-typescript` → model `.d.ts`                                        | Frontend wire and renderer types; no HTTP operation information is needed                           |
| [Haku MCP tool catalog](../../haku/console/mcp/export_tool_schemas.py)                                                                                                                                                                                                                                                         | Draft 2020-12 JSON Schema catalogs for arguments and structured results                                     | `js_json_schema` → `.d.ts` plus retained `.schema.json`; [argument](../../haku/console/frontend/mcp_tool_schema.ts) and [result](../../haku/console/frontend/mcp_tool_result_schema.ts) adapters call `z.fromJSONSchema`            | Typed previews and runtime validators derived from advertised FastMCP contracts                     |
| [Agentplane protobuf definitions](../../agentplane/proto/BUILD.bazel), [Action Service](../../agentplane/action_service/BUILD.bazel), [MCP edge](../../agentplane/mcp_edge/BUILD.bazel)                                                                                                                                        | `.proto` import graph and `ProtoInfo` descriptors                                                           | `protoc` plugins → Protobuf-ES JS/declarations, Go bindings, Python bindings/stubs                                                                                                                                                  | Native messages, service definitions, protobuf binary and ProtoJSON codecs                          |
| [Native gRPC services](../../agentplane/action_service/BUILD.bazel), [runner gateway](../../agentplane/runner_gateway/BUILD.bazel)                                                                                                                                                                                             | Same annotated proto graph                                                                                  | `go_grpc_v2`, [`grpc_gateway`](../go/BUILD.bazel), [`py_grpc_service_library`](../python/grpc.bzl) → Go clients/servers and HTTP gateway; Python service modules and mypy stubs                                                     | Native RPC and HTTP service implementations                                                         |
| [Debundle CP-SAT](../js/debundle/BUILD.bazel)                                                                                                                                                                                                                                                                                  | Pinned OR-Tools `.proto` files                                                                              | `rust_prost_library` → Rust message bindings                                                                                                                                                                                        | Rust solver model and C API interoperability; no schema normalization stage                         |
| Protobuf services with `google.api.http` annotations                                                                                                                                                                                                                                                                           | Same proto graph                                                                                            | [`proto_openapi`](../go/proto_openapi.bzl), `protoc-gen-openapiv2` → Swagger/OpenAPI 2 JSON                                                                                                                                         | Published Action Service, notification, and egress API documentation                                |
| [Extracted Claude hook Zod modules](../claude/claude_api/hooks/schemas/2.1.105/export_json_schema.mjs)                                                                                                                                                                                                                         | Executable Zod schema                                                                                       | `z.toJSONSchema` → Draft 2020-12 JSON Schema                                                                                                                                                                                        | Pydantic/Zod structural-equivalence tests; this is schema publication, not TS generation            |
| [Pinned Codex protocol exporter](../../agentplane/runner/codex/protocol/BUILD.bazel)                                                                                                                                                                                                                                           | JSON Schema documents emitted by the CLI                                                                    | [Quicktype](../../agentplane/runner/codex/protocol/generate.mjs) → Go models                                                                                                                                                        | Runner JSON-RPC protocol implementation                                                             |
| Agentplane Go Kubernetes API types and kubebuilder markers                                                                                                                                                                                                                                                                     | Go package graph                                                                                            | [`controller-gen`](../go/controller_gen.bzl) → DeepCopy Go and CRD YAML; [`kubernetes_clients`](../go/kubernetes_clients.bzl) → Go clients, apply configurations, listers, and informers                                            | Controllers and the [published CRD manifests](../../agentplane/crds/generate.py)                    |
| Agentplane CRDs and pinned upstream CRDs                                                                                                                                                                                                                                                                                       | CRD YAML, sometimes extracted from bundles or Go-embedded schemas by [`extract_crd`](../k8s/extract_crd.py) | [`cdk8s_import`](../js/cdk8s_import.bzl) → Python/jsii construct packages                                                                                                                                                           | Typed cluster configuration and manifest synthesis                                                  |
| [Google Discovery adapter](../../x/google_mcp_server/google_discovery.py)                                                                                                                                                                                                                                                      | Google Discovery JSON plus selected method descriptions                                                     | Runtime adapter → JSON Schema and FastMCP tool definitions                                                                                                                                                                          | Google MCP tools; no generated source file                                                          |
| [OpenAI-compatible Pydantic exporter](../../openai_utils/json_schema.py), [Gmail adapter](../../gmail_archiver/event_classifier.py), [Ollama consumer](../../x/eob_matching/extract_pdfs.py)                                                                                                                                   | Python models                                                                                               | Runtime export → provider-compatible JSON Schema; Ollama receives ordinary Pydantic JSON Schema                                                                                                                                     | Structured-output requests; provider restrictions are part of this boundary                         |
| [Loom answer schemas](../../loom/gym/baseline_llm.py), [Props tool definitions](../../props/agents/af/tools.py)                                                                                                                                                                                                                | Pydantic models → JSON Schema; Loom inlines local references                                                | Component-owned adapters → Inspect/Anthropic tool input schemas                                                                                                                                                                     | Runtime tool advertisement, retaining provider-specific limitations                                 |
| [Operator-authored argument policies](../../agentplane/action_service/policies/argument_schema.py)                                                                                                                                                                                                                             | JSON Schema supplied as policy data                                                                         | Python checks the declared schema dialect; [Go policy evaluation](../../agentplane/action_service/policies/argument_schema.go) compiles and validates with `santhosh-tekuri/jsonschema/v6`                                          | Action argument authorization; no language-model generation or frontend code generation             |
| [Augur rollout vocabulary](../../finance/augur/frontend/export_rollout_event_vocabulary.py)                                                                                                                                                                                                                                    | Backend ordering plus Pydantic discriminator variants                                                       | Component-owned exporter → TS literal tuple and union                                                                                                                                                                               | Frontend ordering and exhaustive vocabulary types                                                   |

### Whole-repository state before

This is one map of the production paths at the design base commit, including the independent
native and runtime paths. Blue boxes are producers, gray boxes are intermediate/output formats,
amber boxes name converters, and green boxes are consumers or checks. Orange boxes identify
the current export/conversion coupling, handwritten aliases, synthetic HTTP wrapper, and
type-stripping path that the proposal changes.

B means build/export time; R means runtime. Boxes describe repository boundary formats, including
in-memory schema/tool objects. R outputs are runtime schemas or objects unless labeled as files.
Generator-internal ASTs and ordinary application payload serialization are outside this map.
Reused format boxes represent separate owner documents unless explicitly labeled as a shared artifact.
The spike-only generators belong to the proof matrix, not this production-before graph.

```mermaid
flowchart LR
    python_models["Python / Pydantic models<br/>owned by each component"]:::producer
    fastmcp_signatures["FastMCP tool signatures<br/>SSH and Haku owners"]:::producer
  subgraph frontend["HTTP, model and tool schemas: generation and frontend consumption"]
    http_export_types["B: js_openapi_schema<br/>invokes owner export_schema.py<br/>app.openapi and owner additions"]:::legacy
    http_types_json["Private OpenAPI 3 JSON<br/>declaration macro invocation"]:::legacy
    http_export_zod["B: js_openapi_zod<br/>invokes owner export_schema.py<br/>app.openapi and owner additions"]:::legacy
    http_zod_json["Separate private OpenAPI 3 JSON<br/>Zod macro invocation"]:::legacy
    api_typegen["B: js_openapi_schema<br/>openapi-typescript"]:::legacy
    api_types["API .d.ts<br/>paths, operations, components"]:::artifact
    model_aliases["Maintained .ts model aliases<br/>components.schemas indexed types"]:::legacy
    zod_gen["B: js_openapi_zod<br/>Hey API Zod 4 plugin"]:::legacy
    python_models --> http_export_types
    http_export_types --> http_types_json
    http_types_json --> api_typegen
    api_typegen --> api_types
    api_types -->|"handwritten aliases"| model_aliases
    python_models --> http_export_zod
    http_export_zod --> http_zod_json
    http_zod_json --> zod_gen
    ssh_reflect["B: SSH tool reflection"]:::converter
    ssh_json["Tool input/output JSON Schemas<br/>in memory"]:::artifact
    ssh_wrap["B: export_frontend_schema.py<br/>invent /exec HTTP operation"]:::legacy
    ssh_openapi["Synthetic OpenAPI 3.1 JSON"]:::legacy
    fastmcp_signatures --> ssh_reflect
    ssh_reflect --> ssh_json
    ssh_json --> ssh_wrap
    ssh_wrap --> ssh_openapi
    ssh_openapi --> zod_gen
    model_export["B: js_json_schema<br/>invokes owner Pydantic exporter<br/>Airlock / Haku serialization mode"]:::legacy
    model_json["Private model JSON Schema"]:::artifact
    tool_export["B: js_json_schema<br/>export_tool_schemas.py<br/>FastMCP catalog selection"]:::legacy
    tool_json["Haku catalog JSON Schemas<br/>Draft 2020-12<br/>arguments and structured results"]:::artifact
    json_typegen["B: js_json_schema<br/>json-schema-to-typescript"]:::legacy
    python_models --> model_export
    model_export --> model_json
    fastmcp_signatures --> tool_export
    tool_export --> tool_json
    model_json --> json_typegen
    tool_json --> json_typegen
    retained_json["Retained catalog .schema.json<br/>js_json_schema schema_out option"]:::artifact
    tool_json -->|"retain for runtime"| retained_json
    zod_compile["B: ts_library / tsc<br/>inside js_openapi_zod<br/>for .ts output"]:::legacy
    strip_ts["B: strip_ts_types<br/>inside js_openapi_zod<br/>for .mjs output"]:::legacy
    zod_mjs["Generated Zod .mjs<br/>no generated declarations"]:::artifact
    js_validation["Study Casino plain-JS<br/>fetch and WebSocket validation"]:::consumer
    zod_ts -->|"Study Casino"| strip_ts
    strip_ts --> zod_mjs
    zod_mjs -->|"js_library / import"| js_validation
    zod_ts["Generated Zod .ts"]:::artifact
    zod_modules["Compiled Zod ES modules<br/>.js and .d.ts"]:::artifact
    zod_validation["Runtime request / response validation<br/>Augur; SSH renderer"]:::consumer
    model_types["Model / catalog .d.ts"]:::artifact
    typed_models["Frontend wire / renderer model types<br/>checked with ts_library / tsc"]:::consumer
    http_clients["Typed HTTP clients<br/>openapi-fetch and operation types"]:::consumer
    json_runtime["R: ordinary TS adapter<br/>z.fromJSONSchema"]:::converter
    runtime_zod["Runtime Zod schema objects"]:::artifact
    tool_consumers["Haku tool previews and validation"]:::consumer
    zod_gen --> zod_ts
    zod_ts -->|"TS output"| zod_compile
    zod_compile --> zod_modules
    zod_modules -->|"import / safeParse"| zod_validation
    json_typegen --> model_types
    model_types -->|"js_library; type-only import"| typed_models
    api_types -->|"paths / operations"| http_clients
    model_aliases -->|"type-only import"| typed_models
    retained_json --> json_runtime
    json_runtime --> runtime_zod
    runtime_zod -->|"safeParse at tested boundary"| tool_consumers
  end
  subgraph native["Native protobuf: unchanged format family"]
    proto_sources["Agentplane .proto source graph<br/>messages; HTTP-annotated services"]:::producer
    proto_export["B: proto_library"]:::converter
    proto_info["ProtoInfo imported source closure<br/>and FileDescriptorSet artifacts"]:::artifact
    proto_es["B: ts_proto_library<br/>protoc-gen-es target=js+dts"]:::converter
    proto_js["Protobuf-ES .js + .d.ts<br/>native message and service schemas"]:::artifact
    proto_go["B: go_proto_library<br/>protoc-gen-go<br/>go_grpc_v2 / protoc-gen-go-grpc<br/>grpc_gateway / protoc-gen-grpc-gateway"]:::converter
    proto_go_src["Go .go sources<br/>messages, gRPC services, HTTP gateway"]:::artifact
    proto_py["B: py_proto_library_with_mypy_stubs<br/>protoc --python_out / --pyi_out<br/>py_grpc_service_library<br/>gRPC + protoc-gen-mypy_grpc"]:::converter
    proto_py_src["Python .py + .pyi<br/>messages and gRPC services"]:::artifact
    native_apps["TS / Go / Python apps and services<br/>native protobuf bytes and ProtoJSON codecs"]:::consumer
    proto_docs["B: proto_openapi<br/>protoc-gen-openapiv2"]:::converter
    swagger["Swagger / OpenAPI 2 JSON<br/>HTTP description; not ProtoJSON parity"]:::artifact
    published_docs["Embedded / published API references<br/>Action, notification and egress APIs"]:::consumer
    ortools["Pinned OR-Tools .proto sources"]:::producer
    ortools_export["B: proto_library"]:::converter
    ortools_info["OR-Tools ProtoInfo source closure<br/>and FileDescriptorSet artifacts"]:::artifact
    prost["B: rust_prost_library<br/>protoc-gen-prost"]:::converter
    rust_src["Rust prost .rs bindings"]:::artifact
    rust_app["Rust solver and C API consumers"]:::consumer
    proto_sources --> proto_export
    proto_export --> proto_info
    proto_info --> proto_es
    proto_es --> proto_js
    proto_info --> proto_go
    proto_go --> proto_go_src
    proto_info --> proto_py
    proto_py --> proto_py_src
    proto_info --> proto_docs
    proto_docs --> swagger
    ortools --> ortools_export
    ortools_export --> ortools_info
    ortools_info --> prost
    prost --> rust_src
    proto_js -->|"js_library; TS typecheck / runtime imports"| native_apps
    proto_go_src -->|"go_library"| native_apps
    proto_py_src -->|"py_library / mypy"| native_apps
    swagger -->|"copy_file; Go embedsrcs"| published_docs
    rust_src -->|"rust_library / Clippy"| rust_app
  end
  subgraph kubernetes["Go API schemas and CRD imports: unchanged format family"]
    go_api["Go Kubernetes API types<br/>kubebuilder markers"]:::producer
    authored_go_path["GoPath copied source closure<br/>authored doc.go and types.go"]:::artifact
    deepcopy["B: controller_deepcopy<br/>controller-gen object"]:::converter
    deepcopy_go["zz_generated.deepcopy.go"]:::artifact
    go_api_package["Go API package / GoPath closure<br/>authored types plus generated methods"]:::artifact
    clientgen["B: kubernetes_clients<br/>applyconfiguration-gen / client-gen<br/>lister-gen / informer-gen"]:::converter
    go_clients["Go client / apply / lister / informer sources"]:::artifact
    controllers["Typed Go controller consumers"]:::consumer
    crdgen["B: controller_crds<br/>controller-gen crd"]:::converter
    generated_crd["Generated structural CRD YAML"]:::artifact
    crd_publish["B: agentplane/crds/generate.py<br/>normalization and schema projection"]:::converter
    published_crd["Published / committed CRD YAML<br/>Agentplane manifests"]:::artifact
    upstream_crds["Pinned upstream CRD YAML / bundles<br/>or Go-embedded schemas"]:::producer
    crd_extract["B: extract_crd<br/>select / unwrap / wrap schema<br/>owner-specific removal policy"]:::converter
    selected_crd["Selected CRD YAML<br/>openAPIV3Schema"]:::artifact
    cdk_import["B: cdk8s_import<br/>cdk8s import / jsii"]:::converter
    cdk_package["Typed Python .py + py.typed<br/>JSII .jsii.tgz runtime package"]:::artifact
    synth["B: Python charts / cdk8s synth<br/>mypy checks construct usage"]:::converter
    manifest_yaml["Synthesized Kubernetes YAML"]:::artifact
    cluster["Typed cluster configuration<br/>Flux / Kubernetes consumers"]:::consumer
    go_api -->|"go_path mode=copy"| authored_go_path
    authored_go_path --> deepcopy
    deepcopy --> deepcopy_go
    go_api --> go_api_package
    deepcopy_go --> go_api_package
    go_api_package --> clientgen
    clientgen --> go_clients
    authored_go_path --> crdgen
    crdgen --> generated_crd
    generated_crd --> crd_publish
    crd_publish --> published_crd
    published_crd --> cdk_import
    upstream_crds --> crd_extract
    crd_extract --> selected_crd
    selected_crd --> cdk_import
    cdk_import --> cdk_package
    cdk_package --> synth
    synth --> manifest_yaml
    manifest_yaml --> cluster
    go_clients -->|"go_library"| controllers
    published_crd -->|"CRD installation"| cluster
    upstream_crds -->|"already a single importable CRD"| cdk_import
  end
  subgraph owners["Owner-specific schema publication and runtime adapters: unchanged"]
    hook_zod["Extracted Claude hook Zod source"]:::producer
    zod_export["B: export_json_schema.mjs<br/>z.toJSONSchema"]:::converter
    hook_json["Draft 2020-12 JSON Schema"]:::artifact
    schema_eq["Python Pydantic / Zod<br/>structural-equivalence checks"]:::consumer
    hook_zod --> zod_export
    zod_export --> hook_json
    hook_json --> schema_eq
    codex_cli["Pinned Codex protocol CLI"]:::producer
    codex_export["B: CLI JSON Schema export"]:::converter
    codex_json["Protocol + JSON-RPC transport<br/>JSON Schema documents"]:::artifact
    codex_select["B: protocol/generate.mjs<br/>select consumed roots / transport adapter"]:::converter
    selected_json["Selected JSON Schema root graph"]:::artifact
    quicktype["B: quicktype-core"]:::converter
    quicktype_go["Generated Go model source"]:::artifact
    runner["Go runner JSON-RPC implementation<br/>model decoding is not schema validation"]:::consumer
    codex_cli --> codex_export
    codex_export --> codex_json
    codex_json --> codex_select
    codex_select --> selected_json
    selected_json --> quicktype
    quicktype --> quicktype_go
    quicktype_go -->|"go_library"| runner
    openai_export["R: openai_json_schema<br/>shared OpenAICompatibleSchema<br/>strict fields / unions / ref siblings"]:::converter
    gmail_export["R: Gmail-local<br/>OpenAICompatibleSchema"]:::converter
    provider_json["Provider-restricted JSON Schemas<br/>separate owner documents / profiles"]:::artifact
    structured_requests["OpenAI-compatible structured requests<br/>schema publication is not live acceptance"]:::consumer
    python_models --> openai_export
    python_models --> gmail_export
    openai_export --> provider_json
    gmail_export --> provider_json
    provider_json --> structured_requests
    runtime_pydantic["R: model_json_schema<br/>owner Pydantic model"]:::converter
    runtime_model_json["Ordinary validation JSON Schema<br/>per owner; may contain local refs"]:::artifact
    ollama["Ollama format schema requests"]:::consumer
    props_adapter["R: Props direct_tool<br/>FunctionTool input_model schema<br/>model_validate at invocation"]:::converter
    props_tools["MAF function tools / typed arguments"]:::consumer
    loom_inline["R: Loom question_schema<br/>_inline_refs"]:::converter
    inline_json["Reference-inlined JSON Schema"]:::artifact
    loom_tools["Inspect / Anthropic tools<br/>Pydantic answer validation"]:::consumer
    python_models --> runtime_pydantic
    runtime_pydantic --> runtime_model_json
    runtime_model_json --> ollama
    runtime_model_json --> props_adapter
    props_adapter --> props_tools
    runtime_model_json --> loom_inline
    loom_inline --> inline_json
    inline_json --> loom_tools
    vocabulary["Augur Pydantic discriminator variants<br/>and explicit backend order"]:::producer
    vocabulary_export["B: export_rollout_event_vocabulary.py<br/>inspect TypeAdapter schema / check order"]:::converter
    vocabulary_ts["TS literal tuple + union source"]:::artifact
    vocabulary_use["Augur ordering and exhaustive types"]:::consumer
    vocabulary --> vocabulary_export
    vocabulary_export --> vocabulary_ts
    vocabulary_ts -->|"ts_library / tsc"| vocabulary_use
    policy_json["Operator-authored JSON Schema<br/>declared dialect"]:::producer
    policy_python["R: Python argument_schema.py<br/>jsonschema dialect / schema check"]:::converter
    policy_admission["Policy schema admission"]:::consumer
    policy_go["R: Go argument_schema.go<br/>santhosh-tekuri/jsonschema/v6 compiler"]:::converter
    compiled_policy["Compiled Go schema validator"]:::artifact
    authorization["Action argument authorization"]:::consumer
    policy_json --> policy_python
    policy_python --> policy_admission
    policy_json --> policy_go
    policy_go --> compiled_policy
    compiled_policy -->|"validate action arguments"| authorization
  end
  subgraph mcp_imports["External schema imports to runtime MCP tools: unchanged"]
    google_wheel["Pinned google-api-python-client wheel"]:::producer
    discovery_load["R: _load_doc<br/>importlib.resources"]:::converter
    discovery_json["Google Discovery JSON<br/>legacy schema dialect + Google extensions"]:::artifact
    gentool_specs["GenTool expose / pin / method specs"]:::producer
    discovery_map["R: build_generated_tools<br/>_to_json_schema / input / output mapping<br/>cycles elided; int64 stays string"]:::converter
    google_tool_objects["FastMCP GeneratedGoogleTool objects<br/>input and result JSON Schemas"]:::artifact
    grocy_raw["Pinned Grocy OpenAPI 3.1 JSON"]:::producer
    grocy_repair["B: grocy_openapi_fixed<br/>fix_openapi_spec.py"]:::converter
    grocy_fixed["Grocy-repaired OpenAPI 3.1 JSON"]:::artifact
    grocy_provider["R: FastMCP OpenAPIProvider<br/>server.build_mcp"]:::converter
    grocy_objects["Generated OpenAPI-backed tool objects"]:::artifact
    grocy_customize["R: Grocy owner customization<br/>names / descriptions / drop output schemas"]:::converter
    grocy_input_tools["Grocy tool objects<br/>input schema only"]:::artifact
    scope_clients["R: RequestScopedOpenAPIClients<br/>bind caller HTTP client per invocation"]:::converter
    grocy_http["Grocy HTTP tool execution"]:::consumer
    advertise["R: FastMCP tool advertisement"]:::converter
    advertised_json["MCP tool descriptors in JSON<br/>input schemas; output schemas if retained"]:::artifact
    mcp_clients["Google / Grocy MCP clients"]:::consumer
    google_wheel --> discovery_load
    discovery_load --> discovery_json
    discovery_json --> discovery_map
    gentool_specs --> discovery_map
    discovery_map --> google_tool_objects
    google_tool_objects --> advertise
    grocy_raw --> grocy_repair
    grocy_repair --> grocy_fixed
    grocy_fixed --> grocy_provider
    grocy_provider --> grocy_objects
    grocy_objects --> grocy_customize
    grocy_customize --> grocy_input_tools
    grocy_input_tools --> advertise
    grocy_input_tools --> scope_clients
    scope_clients --> grocy_http
    advertise --> advertised_json
    advertised_json --> mcp_clients
  end
  classDef producer fill:#dbeafe,stroke:#2563eb,color:#172554
  classDef artifact fill:#f1f5f9,stroke:#64748b,color:#0f172a
  classDef converter fill:#fef3c7,stroke:#b45309,color:#451a03
  classDef consumer fill:#dcfce7,stroke:#15803d,color:#052e16
  classDef legacy fill:#ffedd5,stroke:#c2410c,color:#431407
  classDef proposed fill:#f3e8ff,stroke:#9333ea,color:#3b0764
```

The baseline also records independent runtime-validator and protobuf proposals. This migration
uses the production paths and artifacts described below; it does not import behavior from those
drafts or expand into native protobuf generation.

## Proposed end state

### Whole-repository state after

This is the complete target state, with every native and owner-specific path retained. Purple boxes
mark file-input converters, explicit schema artifacts, named model exports, and ordinary compilation
of generated Zod. The unchanged paths are repeated in full so this map stands on its own. It
introduces no universal schema IR: OpenAPI 3, standalone JSON Schema, protobuf descriptors, Swagger
2, Discovery, and Kubernetes structural schemas stay distinct. The converter and owner-artifact
migration is implemented, and [PR #12418](https://github.com/agentydragon/ducktape/pull/12418)
migrated maintained model consumers to direct imports from generated named exports.

The HTTP owner exports one named OpenAPI artifact for both declarations and Zod. Model/tool owners
export standalone JSON Schema directly, including SSH; the synthetic HTTP operation disappears.
Generated named imports replace maintained model aliases where naming checks pass. Both TS and
plain-JS validation consumers use Zod modules emitted by normal TypeScript compilation.

The OpenAPI and JSON Schema declaration macros expose a `js_library` named by the macro call and keep
the raw `.d.ts` addressable by its output label. A runtime package that also needs schema JSON keeps
that file in its own `js_library` and adds the generated declaration library through `deps`.
`openapi_zod` stays file-only; its consumer supplies compiler settings and runtime dependencies in
an explicit `ts_library`.

```mermaid
flowchart LR
    python_models["Python / Pydantic models<br/>owned by each component"]:::producer
    fastmcp_signatures["FastMCP tool signatures<br/>SSH and Haku owners"]:::producer
  subgraph frontend["HTTP, model and tool schemas: generation and frontend consumption"]
    http_export["B: owner export target<br/>export_schema.py / app.openapi<br/>and owner CSV or WebSocket additions"]:::proposed
    http_json["Declared OpenAPI 3 JSON<br/>one shared artifact per HTTP owner"]:::proposed
    api_typegen["B: openapi_typescript(name, schema, out)<br/>openapi-typescript + root-type flags"]:::proposed
    api_types["API .d.ts<br/>paths, operations, components<br/>named model exports; raw :out file label"]:::proposed
    api_types_library["Named js_library from the macro<br/>JsInfo.transitive_types"]:::proposed
    zod_gen["B: openapi_zod(name, schema, config, out)<br/>Hey API Zod 4 plugin"]:::proposed
    python_models --> http_export
    http_export --> http_json
    http_json --> api_typegen
    api_typegen --> api_types
    api_types --> api_types_library
    http_json --> zod_gen
    model_export["B: owner model export target<br/>Pydantic / TypeAdapter<br/>explicit serialization or validation mode"]:::proposed
    model_json["Declared model JSON Schema<br/>mode and dialect are explicit"]:::proposed
    tool_export["B: owner tool-schema export target<br/>FastMCP reflection / catalog selection"]:::proposed
    tool_json["Declared tool JSON Schemas<br/>SSH input/output; Haku catalogs<br/>arguments and structured results"]:::proposed
    json_typegen["B: json_schema_typescript(name, schema, out)<br/>json-schema-to-typescript"]:::proposed
    python_models --> model_export
    model_export --> model_json
    fastmcp_signatures --> tool_export
    tool_export --> tool_json
    model_json --> json_typegen
    tool_json --> json_typegen
    zod_compile["B: ordinary ts_library / tsc<br/>compile generated Zod source"]:::proposed
    zod_ts["Generated Zod .ts"]:::artifact
    zod_modules["Compiled Zod ES modules<br/>.js and .d.ts"]:::artifact
    zod_validation["Runtime request / response validation<br/>Augur and Study Casino<br/>TS and plain-JS consumers"]:::consumer
    model_types["Model / catalog .d.ts<br/>raw :out file label"]:::artifact
    model_types_library["Named js_library from the macro<br/>JsInfo.transitive_types"]:::proposed
    typed_models["Frontend wire / renderer model types<br/>checked with ts_library / tsc"]:::consumer
    http_clients["Typed HTTP clients<br/>openapi-fetch and operation types"]:::consumer
    json_runtime["R: ordinary TS adapter<br/>z.fromJSONSchema"]:::converter
    runtime_zod["Runtime Zod schema objects"]:::artifact
    tool_consumers["Haku tool previews and validation<br/>SSH action rendering"]:::consumer
    tool_package["B: js_library wrapper<br/>runtime JSON srcs + declaration-library deps"]:::proposed
    zod_gen --> zod_ts
    zod_ts -->|"TS output"| zod_compile
    zod_compile --> zod_modules
    zod_modules -->|"import / safeParse"| zod_validation
    json_typegen --> model_types
    model_types --> model_types_library
    model_types_library -->|"type-only imports"| typed_models
    api_types_library -->|"paths / operations"| http_clients
    api_types_library -->|"direct named imports; no alias module"| typed_models
    tool_json --> tool_package
    model_types_library -->|"deps"| tool_package
    tool_json -->|"SSH direct JSON"| json_runtime
    tool_package -->|"Haku JSON imports; types via deps"| json_runtime
    model_json -->|"where runtime validation is needed"| json_runtime
    json_runtime --> runtime_zod
    runtime_zod -->|"safeParse at tested boundary"| tool_consumers
  end
  subgraph native["Native protobuf: unchanged format family"]
    proto_sources["Agentplane .proto source graph<br/>messages; HTTP-annotated services"]:::producer
    proto_export["B: proto_library"]:::converter
    proto_info["ProtoInfo imported source closure<br/>and FileDescriptorSet artifacts"]:::artifact
    proto_es["B: ts_proto_library<br/>protoc-gen-es target=js+dts"]:::converter
    proto_js["Protobuf-ES .js + .d.ts<br/>native message and service schemas"]:::artifact
    proto_go["B: go_proto_library<br/>protoc-gen-go<br/>go_grpc_v2 / protoc-gen-go-grpc<br/>grpc_gateway / protoc-gen-grpc-gateway"]:::converter
    proto_go_src["Go .go sources<br/>messages, gRPC services, HTTP gateway"]:::artifact
    proto_py["B: py_proto_library_with_mypy_stubs<br/>protoc --python_out / --pyi_out<br/>py_grpc_service_library<br/>gRPC + protoc-gen-mypy_grpc"]:::converter
    proto_py_src["Python .py + .pyi<br/>messages and gRPC services"]:::artifact
    native_apps["TS / Go / Python apps and services<br/>native protobuf bytes and ProtoJSON codecs"]:::consumer
    proto_docs["B: proto_openapi<br/>protoc-gen-openapiv2"]:::converter
    swagger["Swagger / OpenAPI 2 JSON<br/>HTTP description; not ProtoJSON parity"]:::artifact
    published_docs["Embedded / published API references<br/>Action, notification and egress APIs"]:::consumer
    ortools["Pinned OR-Tools .proto sources"]:::producer
    ortools_export["B: proto_library"]:::converter
    ortools_info["OR-Tools ProtoInfo source closure<br/>and FileDescriptorSet artifacts"]:::artifact
    prost["B: rust_prost_library<br/>protoc-gen-prost"]:::converter
    rust_src["Rust prost .rs bindings"]:::artifact
    rust_app["Rust solver and C API consumers"]:::consumer
    proto_sources --> proto_export
    proto_export --> proto_info
    proto_info --> proto_es
    proto_es --> proto_js
    proto_info --> proto_go
    proto_go --> proto_go_src
    proto_info --> proto_py
    proto_py --> proto_py_src
    proto_info --> proto_docs
    proto_docs --> swagger
    ortools --> ortools_export
    ortools_export --> ortools_info
    ortools_info --> prost
    prost --> rust_src
    proto_js -->|"js_library; TS typecheck / runtime imports"| native_apps
    proto_go_src -->|"go_library"| native_apps
    proto_py_src -->|"py_library / mypy"| native_apps
    swagger -->|"copy_file; Go embedsrcs"| published_docs
    rust_src -->|"rust_library / Clippy"| rust_app
  end
  subgraph kubernetes["Go API schemas and CRD imports: unchanged format family"]
    go_api["Go Kubernetes API types<br/>kubebuilder markers"]:::producer
    authored_go_path["GoPath copied source closure<br/>authored doc.go and types.go"]:::artifact
    deepcopy["B: controller_deepcopy<br/>controller-gen object"]:::converter
    deepcopy_go["zz_generated.deepcopy.go"]:::artifact
    go_api_package["Go API package / GoPath closure<br/>authored types plus generated methods"]:::artifact
    clientgen["B: kubernetes_clients<br/>applyconfiguration-gen / client-gen<br/>lister-gen / informer-gen"]:::converter
    go_clients["Go client / apply / lister / informer sources"]:::artifact
    controllers["Typed Go controller consumers"]:::consumer
    crdgen["B: controller_crds<br/>controller-gen crd"]:::converter
    generated_crd["Generated structural CRD YAML"]:::artifact
    crd_publish["B: agentplane/crds/generate.py<br/>normalization and schema projection"]:::converter
    published_crd["Published / committed CRD YAML<br/>Agentplane manifests"]:::artifact
    upstream_crds["Pinned upstream CRD YAML / bundles<br/>or Go-embedded schemas"]:::producer
    crd_extract["B: extract_crd<br/>select / unwrap / wrap schema<br/>owner-specific removal policy"]:::converter
    selected_crd["Selected CRD YAML<br/>openAPIV3Schema"]:::artifact
    cdk_import["B: cdk8s_import<br/>cdk8s import / jsii"]:::converter
    cdk_package["Typed Python .py + py.typed<br/>JSII .jsii.tgz runtime package"]:::artifact
    synth["B: Python charts / cdk8s synth<br/>mypy checks construct usage"]:::converter
    manifest_yaml["Synthesized Kubernetes YAML"]:::artifact
    cluster["Typed cluster configuration<br/>Flux / Kubernetes consumers"]:::consumer
    go_api -->|"go_path mode=copy"| authored_go_path
    authored_go_path --> deepcopy
    deepcopy --> deepcopy_go
    go_api --> go_api_package
    deepcopy_go --> go_api_package
    go_api_package --> clientgen
    clientgen --> go_clients
    authored_go_path --> crdgen
    crdgen --> generated_crd
    generated_crd --> crd_publish
    crd_publish --> published_crd
    published_crd --> cdk_import
    upstream_crds --> crd_extract
    crd_extract --> selected_crd
    selected_crd --> cdk_import
    cdk_import --> cdk_package
    cdk_package --> synth
    synth --> manifest_yaml
    manifest_yaml --> cluster
    go_clients -->|"go_library"| controllers
    published_crd -->|"CRD installation"| cluster
    upstream_crds -->|"already a single importable CRD"| cdk_import
  end
  subgraph owners["Owner-specific schema publication and runtime adapters: unchanged"]
    hook_zod["Extracted Claude hook Zod source"]:::producer
    zod_export["B: export_json_schema.mjs<br/>z.toJSONSchema"]:::converter
    hook_json["Draft 2020-12 JSON Schema"]:::artifact
    schema_eq["Python Pydantic / Zod<br/>structural-equivalence checks"]:::consumer
    hook_zod --> zod_export
    zod_export --> hook_json
    hook_json --> schema_eq
    codex_cli["Pinned Codex protocol CLI"]:::producer
    codex_export["B: CLI JSON Schema export"]:::converter
    codex_json["Protocol + JSON-RPC transport<br/>JSON Schema documents"]:::artifact
    codex_select["B: protocol/generate.mjs<br/>select consumed roots / transport adapter"]:::converter
    selected_json["Selected JSON Schema root graph"]:::artifact
    quicktype["B: quicktype-core"]:::converter
    quicktype_go["Generated Go model source"]:::artifact
    runner["Go runner JSON-RPC implementation<br/>model decoding is not schema validation"]:::consumer
    codex_cli --> codex_export
    codex_export --> codex_json
    codex_json --> codex_select
    codex_select --> selected_json
    selected_json --> quicktype
    quicktype --> quicktype_go
    quicktype_go -->|"go_library"| runner
    openai_export["R: openai_json_schema<br/>shared OpenAICompatibleSchema<br/>strict fields / unions / ref siblings"]:::converter
    gmail_export["R: Gmail-local<br/>OpenAICompatibleSchema"]:::converter
    provider_json["Provider-restricted JSON Schemas<br/>separate owner documents / profiles"]:::artifact
    structured_requests["OpenAI-compatible structured requests<br/>schema publication is not live acceptance"]:::consumer
    python_models --> openai_export
    python_models --> gmail_export
    openai_export --> provider_json
    gmail_export --> provider_json
    provider_json --> structured_requests
    runtime_pydantic["R: model_json_schema<br/>owner Pydantic model"]:::converter
    runtime_model_json["Ordinary validation JSON Schema<br/>per owner; may contain local refs"]:::artifact
    ollama["Ollama format schema requests"]:::consumer
    props_adapter["R: Props direct_tool<br/>FunctionTool input_model schema<br/>model_validate at invocation"]:::converter
    props_tools["MAF function tools / typed arguments"]:::consumer
    loom_inline["R: Loom question_schema<br/>_inline_refs"]:::converter
    inline_json["Reference-inlined JSON Schema"]:::artifact
    loom_tools["Inspect / Anthropic tools<br/>Pydantic answer validation"]:::consumer
    python_models --> runtime_pydantic
    runtime_pydantic --> runtime_model_json
    runtime_model_json --> ollama
    runtime_model_json --> props_adapter
    props_adapter --> props_tools
    runtime_model_json --> loom_inline
    loom_inline --> inline_json
    inline_json --> loom_tools
    vocabulary["Augur Pydantic discriminator variants<br/>and explicit backend order"]:::producer
    vocabulary_export["B: export_rollout_event_vocabulary.py<br/>inspect TypeAdapter schema / check order"]:::converter
    vocabulary_ts["TS literal tuple + union source"]:::artifact
    vocabulary_use["Augur ordering and exhaustive types"]:::consumer
    vocabulary --> vocabulary_export
    vocabulary_export --> vocabulary_ts
    vocabulary_ts -->|"ts_library / tsc"| vocabulary_use
    policy_json["Operator-authored JSON Schema<br/>declared dialect"]:::producer
    policy_python["R: Python argument_schema.py<br/>jsonschema dialect / schema check"]:::converter
    policy_admission["Policy schema admission"]:::consumer
    policy_go["R: Go argument_schema.go<br/>santhosh-tekuri/jsonschema/v6 compiler"]:::converter
    compiled_policy["Compiled Go schema validator"]:::artifact
    authorization["Action argument authorization"]:::consumer
    policy_json --> policy_python
    policy_python --> policy_admission
    policy_json --> policy_go
    policy_go --> compiled_policy
    compiled_policy -->|"validate action arguments"| authorization
  end
  subgraph mcp_imports["External schema imports to runtime MCP tools: unchanged"]
    google_wheel["Pinned google-api-python-client wheel"]:::producer
    discovery_load["R: _load_doc<br/>importlib.resources"]:::converter
    discovery_json["Google Discovery JSON<br/>legacy schema dialect + Google extensions"]:::artifact
    gentool_specs["GenTool expose / pin / method specs"]:::producer
    discovery_map["R: build_generated_tools<br/>_to_json_schema / input / output mapping<br/>cycles elided; int64 stays string"]:::converter
    google_tool_objects["FastMCP GeneratedGoogleTool objects<br/>input and result JSON Schemas"]:::artifact
    grocy_raw["Pinned Grocy OpenAPI 3.1 JSON"]:::producer
    grocy_repair["B: grocy_openapi_fixed<br/>fix_openapi_spec.py"]:::converter
    grocy_fixed["Grocy-repaired OpenAPI 3.1 JSON"]:::artifact
    grocy_provider["R: FastMCP OpenAPIProvider<br/>server.build_mcp"]:::converter
    grocy_objects["Generated OpenAPI-backed tool objects"]:::artifact
    grocy_customize["R: Grocy owner customization<br/>names / descriptions / drop output schemas"]:::converter
    grocy_input_tools["Grocy tool objects<br/>input schema only"]:::artifact
    scope_clients["R: RequestScopedOpenAPIClients<br/>bind caller HTTP client per invocation"]:::converter
    grocy_http["Grocy HTTP tool execution"]:::consumer
    advertise["R: FastMCP tool advertisement"]:::converter
    advertised_json["MCP tool descriptors in JSON<br/>input schemas; output schemas if retained"]:::artifact
    mcp_clients["Google / Grocy MCP clients"]:::consumer
    google_wheel --> discovery_load
    discovery_load --> discovery_json
    discovery_json --> discovery_map
    gentool_specs --> discovery_map
    discovery_map --> google_tool_objects
    google_tool_objects --> advertise
    grocy_raw --> grocy_repair
    grocy_repair --> grocy_fixed
    grocy_fixed --> grocy_provider
    grocy_provider --> grocy_objects
    grocy_objects --> grocy_customize
    grocy_customize --> grocy_input_tools
    grocy_input_tools --> advertise
    grocy_input_tools --> scope_clients
    scope_clients --> grocy_http
    advertise --> advertised_json
    advertised_json --> mcp_clients
  end
  classDef producer fill:#dbeafe,stroke:#2563eb,color:#172554
  classDef artifact fill:#f1f5f9,stroke:#64748b,color:#0f172a
  classDef converter fill:#fef3c7,stroke:#b45309,color:#451a03
  classDef consumer fill:#dcfce7,stroke:#15803d,color:#052e16
  classDef legacy fill:#ffedd5,stroke:#c2410c,color:#431407
  classDef proposed fill:#f3e8ff,stroke:#9333ea,color:#3b0764
```

### Export once, using the owner's real contract

An exporter owns construction of the app, model selection, FastMCP reflection, serialization mode,
and any component-specific vocabulary. Its public Bazel output is an explicit schema file or
schema graph. A Python binary writing JSON to stdout is an execution mechanism, not an input format
for every converter.

Expose separate targets for OpenAPI documents and standalone model/tool JSON Schemas. Generated
files need named Bazel outputs with visibility for their consumers, not `exports_files` declarations
or a global schema registry. Publish schema artifacts only where independent consumers need them;
keep exporter executable dependencies behind the owning target. Name the
direction where relevant: request validation schema versus serialized response schema. Keep
Python/Pydantic and Go API definitions remain the sources of truth; the implementation adds no
generated Pydantic-model layer.

OpenAPI declarations and Zod generation should share one `:openapi` artifact. This removes duplicate
export actions and makes the schema available to independent validators and documentation consumers.
Non-HTTP schemas, such as MCP arguments/results, should remain JSON Schema rather than acquire fake
HTTP routes to satisfy a generator.

### Converters consume declared files, not backend executables

The interfaces below describe conversion only. These format-specific file-input rules are the
production interfaces, documented in [SCHEMA_CODEGEN.md](../js/SCHEMA_CODEGEN.md). They use Bazel
`$(location ...)` expansion so source, generated, and cross-package single-file target labels work
without parsing labels in Starlark. The fixtures prove generated cross-package schema/config targets,
declared relative external references, and imported config helper inputs.

| Conversion                             | Rule or mechanism                                                                                   | Output and next stage                                                                                                                      |
| -------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| OpenAPI 3.0/3.1 → API declarations     | `openapi_typescript(name, schema, out)`, using pinned `openapi-typescript` with named root exports  | `.d.ts` remains addressable by `out`; `name` is the `js_library` exposing `JsInfo.transitive_types` to ordinary `ts_library` modules       |
| OpenAPI 3.0/3.1 → Zod implementation   | File-only `openapi_zod(name, schema, config, out)`, using pinned `@hey-api/openapi-ts` Zod 4 plugin | `.ts`; compile with an explicit `ts_library` for compiler settings and deps to JS plus declarations; JS-only consumers import emitted JS   |
| JSON Schema → model declarations       | `json_schema_typescript(name, schema, out)`, using pinned `json-schema-to-typescript`               | `.d.ts` remains addressable by `out`; `name` is the `js_library` exposing `JsInfo.transitive_types`                                        |
| JSON Schema → runtime Zod              | Retained JSON plus a normal TS adapter using pinned `z.fromJSONSchema`                              | Runtime validator; generated declarations supply static model types. Keep any assertion joining those two artifacts at one tested boundary |
| Zod → JSON Schema                      | A component-owned JS exporter using `z.toJSONSchema`, invoked with `js_run_binary`                  | Explicitly targeted Draft 2020-12 JSON; choose input/output direction and fail on unrepresentable features                                 |
| Protobuf → language bindings           | `proto_library`/`ProtoInfo` plus language-specific `protoc` rules/plugins                           | Protobuf-ES JS/declarations, Go, Python/stubs, Rust prost; retain native runtime descriptors and codecs                                    |
| Protobuf → RPC service/gateway code    | Existing Go gRPC/gateway compilers and typed Python gRPC macro                                      | Go/Python service code and Go HTTP gateway; normal libraries and consumers                                                                 |
| Protobuf HTTP services → documentation | Existing `proto_openapi`                                                                            | OpenAPI 2 JSON, kept separate from the OpenAPI 3 TS pipeline                                                                               |
| JSON Schema → Go                       | Pinned Quicktype with a component-owned selection/adapter stage                                     | Go source; normal `go_library` and Go tests compile and consume it                                                                         |
| JSON Schema → Go runtime validator     | Existing `santhosh-tekuri/jsonschema/v6` compiler with declared resources and dialect policy        | Runtime validation; generated Go model decoding is not a substitute for schema constraints                                                 |
| Go API types → Kubernetes artifacts    | Existing controller/client generators                                                               | CRD YAML and Go source; publish manifests separately from compilation                                                                      |
| CRD → Python constructs                | Existing `cdk8s_import`                                                                             | Python/jsii package; normal Python type checking and synthesis tests                                                                       |
| OpenAPI → MCP schema/tools             | Existing FastMCP `OpenAPIProvider`, with component-owned repairs and request-scoped client adapter  | Runtime tool schemas and HTTP tools; do not route through TS generation                                                                    |
| Discovery JSON → MCP schema            | Existing Google Discovery adapter                                                                   | Runtime tool JSON Schema; validate the actual advertised schema and sample arguments                                                       |
| Pydantic → provider schema             | Existing OpenAI-compatible exporter                                                                 | Runtime JSON Schema; provider-subset validation stays with the exporter                                                                    |
| Pydantic → flattened tool schema       | Existing owner-specific adapter, including Loom reference inlining                                  | Runtime JSON Schema; retain the referenced schema for consumers supporting it                                                              |
| Backend vocabulary → TS values/types   | Component-owned exporter                                                                            | `.ts`; ordinary `ts_library` compilation                                                                                                   |

File labels, referenced-schema files, tool configuration, and all generator dependencies must be
declared action inputs. No converter should fetch remote references during a build. JSON Schema
dialect and reference-resolution policy are explicit contracts, not inferred from a `.json` suffix.
A general recursive reference resolver or schema normalization framework is not proposed here.

### Generate named model exports

The operation map remains necessary for typed HTTP calls, while model consumers can write
`import type { InvoiceResponse } from "./api/schema"`. The generator owns these direct named exports.
[PR #12418](https://github.com/agentydragon/ducktape/pull/12418) removed maintained alias-only
source files and migrated their consumers to direct imports.

The spike exercises the existing `openapi-typescript` root-export options:

```text
--root-types --root-types-no-schema-prefix
```

These request unprefixed model exports while retaining `paths` and `components`. Typed consumers
import generated model names directly; a handwritten forwarding module would leave the same
maintenance burden in a different file. The generator normalizes names, including Pydantic's
`InvoiceLine-Input` and `InvoiceLine-Output` components.

Do not enable `--root-types-keep-casing` globally: the pinned generator emits invalid identifiers
for those hyphenated names. Normalization has its own limit: `HTTPTrace` and `HttpTrace` collide.
Compile generated declarations and their direct imports before migrating each caller. Keep the
indexed component namespace for exceptional collisions until a naming policy is proven; a custom
alias generator is only justified if a required case cannot use the upstream options. The spike
checks supported names and compiler failures rather than claiming arbitrary schemas are safe.

### Language compilation stays in language rules

`tsconfig`, declaration emission, module format, and `isolated_typecheck` belong to `ts_library`.
The OpenAPI converter should not select `ts_library` versus `js_library` by inspecting `out`'s
extension, strip types itself, or accept unrelated backend-export options. Generated Zod is TS
implementation source; its runnable JS and declarations come from normal compilation. The spike
includes a JavaScript-only import so the Study Casino case has a real consumer.

The same distinction applies to Go and Python: code generation produces language artifacts; normal
libraries and tests prove that those artifacts resolve and compile with their real dependencies.
Generated files remain Bazel outputs, except independently published manifests that their owner
already checks into Git.

### Preserve semantics at each boundary

- API operation types and model-only types are distinct outputs. Standalone JSON Schema cannot
  carry an OpenAPI operation's URL, HTTP method, or parameter locations.
- Missing, nullable, and defaulted fields are different. Test both accepted and rejected values;
  do not equate a successful conversion with equivalent defaults or unknown-property behavior.
- Pydantic validation and serialization schemas may differ, including Decimal representation.
  Test serialized producer data against the corresponding generated consumer.
- Datetimes on the wire remain strings, including offsets; timestamp parsing policy is explicit.
- ProtoJSON has its own int64, Timestamp, enum, oneof, and presence semantics. Use protobuf codecs,
  preserve large integers, and handle future numeric enum values explicitly in consuming adapters.
- JSON Schema and Zod describe a structural subset of Python behavior. Custom validators,
  transformations, and arbitrary refinements do not become portable through schema export.
- OpenAPI 2 publication, Kubernetes structural schemas, and provider-restricted JSON Schema retain
  their own adapters. In the protobuf fixture, Swagger lists symbolic enum strings and flattens
  oneof and optional-presence constraints; native ProtoJSON codecs carry the runtime semantics.
  A universal conversion through OpenAPI or JSON Schema is not required.

## Executable proof matrix

The fixtures are deliberately small independent models, not copies of production generated output.
Each generation step runs during Bazel builds; each runtime test consumes the generated artifact.
Static consumers are compiled against generated types. Tests check boundary behavior and important
wire distinctions, rather than generated-source spelling or generator implementation details.

| Case                       | Small source → generated intermediate → consumer                                                                                        | Checks                                                                                                                                                                                                     |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API declarations and Zod   | Pydantic/FastAPI fixture and generated cross-package schemas/config → production OpenAPI/JSON Schema converters → typed TS and plain JS | Direct named model imports; declared relative schema/config references; operation-map typing; Pydantic names; runtime valid/invalid response parsing, discriminators, missing/null, and caller date policy |
| Production reference guard | OpenAPI and JSON Schema documents with remote refs, plus local file inputs with absolute `$id` → all three production launchers         | Remote-reference conversions fail with zero loopback HTTP requests; local files do not fetch                                                                                                               |
| Model/tool JSON Schema     | Pydantic fixture → standalone JSON Schema → declarations plus retained JSON → runtime Zod                                               | TS consumer type checking; producer payload acceptance and malformed payload rejection                                                                                                                     |
| Reverse schema publication | Zod source → Draft 2020-12 JSON Schema → Python validator                                                                               | Node export/import succeeds; independent Python validation of accepted and rejected values                                                                                                                 |
| Native protobuf            | Two imported proto files → JS/declarations, Go, Python; common proto → Rust prost → consumers                                           | TS, Go, Python, and Rust checks; producer/consumer JSON interoperability; int64, Timestamp, enum, oneof, optional presence                                                                                 |
| Native services            | Same annotated proto → Go/Python gRPC service bindings and Go gateway → consumers                                                       | Typed Python stub construction; Go fake-connection invocation; gateway compile coverage                                                                                                                    |
| Protobuf documentation     | HTTP-annotated proto → Swagger JSON → document consumer                                                                                 | Actual operation and schema semantics, independently of generated language bindings                                                                                                                        |
| Schema → Go                | Tiny protocol JSON Schema → Quicktype Go → typed Go consumer; same schema → native Go JSON Schema validator                             | Go compilation and JSON decoding behavior; reject a schema constraint that model decoding alone accepts                                                                                                    |
| Kubernetes schema/bindings | Tiny annotated Go API → CRD YAML → Python/jsii construct → synthesized object                                                           | Go compilation, CRD content, Python type checking and synthesis; clientset construction, other generated clients compile only                                                                              |
| OpenAPI tool import        | Small OpenAPI document → real FastMCP provider → advertised tool schemas                                                                | Ref/parameter mapping and valid/invalid tool arguments, with no external service                                                                                                                           |
| Flattened provider schema  | Nested Pydantic model → real Loom adapter → inline JSON Schema                                                                          | Reference-free schema and independent accepted/rejected instance checks; no live provider                                                                                                                  |
| Discovery tool import      | Tiny Discovery document → real adapter → FastMCP advertised JSON Schema                                                                 | Independent schema validation and valid/invalid tool argument checks; no Google network call                                                                                                               |
| Provider schema export     | Tiny Pydantic model → real provider-specific exporter → JSON Schema consumer                                                            | Exported structural contract and representative payload validation; no live provider-acceptance claim                                                                                                      |
| Vocabulary export          | Backend variants/order → TS values and union → typed TS consumer                                                                        | Runtime order and static vocabulary contract                                                                                                                                                               |

The source and target definitions live in [TypeScript](../x/schema_codegen/typescript/BUILD.bazel),
[protobuf](../x/schema_codegen/protobuf/BUILD.bazel), and the infrastructure subpackages
([OpenAPI tools](../x/schema_codegen/infrastructure/grocy_openapi/BUILD.bazel),
[Kubernetes](../x/schema_codegen/infrastructure/kubernetes/BUILD.bazel),
[Quicktype](../x/schema_codegen/infrastructure/quicktype/BUILD.bazel),
[Discovery](../x/schema_codegen/infrastructure/discovery/BUILD.bazel),
[Pydantic](../x/schema_codegen/infrastructure/pydantic/BUILD.bazel)). The reference-inlining check
lives beside its private owning adapter in [Loom](../../loom/gym/test_schema_codegen.py).

```bash
bbr test //devinfra/x/schema_codegen/... //loom/gym:test_schema_codegen --test_output=errors
```

Selecting all labels runs TS compilation, Go and Rust compilation/tests, and Python mypy/ruff
aspects on the selected libraries as well as runtime tests. The shorter
`bbr test //devinfra/x/schema_codegen:tests --test_output=errors` selects the aggregate runtime
suite; it does not independently select every Python library's lint/type aspects.

The full proof suite passed all 22 test targets after analyzing 178 Bazel targets in
[BuildBuddy invocation e071545e](https://app.buildbuddy.io/invocation/e071545e-958b-4f0b-b275-4a66fad2fc4d).
Changed-file pre-commit hooks passed. The test-only reference guard receives the three production
launcher binaries through its `js_test` data, so it checks the same entry points used by conversion
rules.

A successful synthetic fixture proves that conversion path and tool composition; it does
not establish equivalence for every schema in a production corpus. OpenAPI fixtures use 3.1 and
cover both local component references and a declared relative external file reference across
packages. OpenAPI 3.0 and larger nested external-reference graphs are not tested here.
Rust covers the simple common message and enum; the cross-language JSON wire tests cover TS, Go,
and Python. No live RPC server, Kubernetes API, Google service, or LLM provider is needed.

## Migration gates and sequence

The implementation burned down the proposed migration gates as follows:

1. Promoted the OpenAPI declarations, OpenAPI-to-Zod, and JSON Schema-to-declarations converters
   into the format-specific file-input rules in [#12477](https://github.com/agentydragon/ducktape/pull/12477),
   [#12484](https://github.com/agentydragon/ducktape/pull/12484), and
   [#12489](https://github.com/agentydragon/ducktape/pull/12489). [#12499](https://github.com/agentydragon/ducktape/pull/12499)
   makes the OpenAPI and JSON Schema macros expose those declarations through named `js_library`
   targets while preserving direct output-file labels. Tests exercise source and generated single-file
   labels, cross-package schemas and configuration, declared relative references, and caller date
   settings.
2. Migrated every production caller of the previous `js_openapi_schema`, `js_openapi_zod`, and
   `js_json_schema` macros to explicit owner schema artifacts and normal language packaging. The
   SSH owner exports standalone argument/result schemas, and its actual FastMCP producer payloads
   pass through the renderer validators. Study Casino now compiles generated Zod through its normal
   TypeScript library path.
3. Preserved OpenAPI operation maps, runtime validators, schema output names, timestamp policies,
   and owner-specific adaptations. The spike's `js_test` passes all three production launchers as
   data and proves remote references issue no requests while local references and absolute `$id`
   file inputs work.
4. Deleted the superseded executable-input macros and `strip_ts_types` after the final callers
   migrated. Their old definitions are linked from the immutable baseline inventory above.
5. Validated generated root model exports and migrated maintained indexed-model consumers to direct
   imports in [PR #12418](https://github.com/agentydragon/ducktape/pull/12418). Exceptional derived
   types and naming-collision cases remain owner-maintained.

The implementation keeps the existing wire contracts and pinned generator versions. The selected
production schema targets, owning renderer tests, frontend type/lint targets, and changed-file hooks
are the acceptance gate; PR CI checks the published stacked revisions. Native protobuf/RPC,
Swagger 2, Kubernetes, Discovery, provider/MCP owner paths, and unrelated runtime validators remain
outside this migration.

Grocy spec repairs, provider-specific reference inlining, and schema restrictions remain with their
owning adapters. Consolidating the older Gmail exporter is a separate caller migration; the spike
uses the shared exporter without claiming identical behavior for every adapter.

## Tool references

Versions come from [`package.json`](../../package.json), [`pnpm-lock.yaml`](../../pnpm-lock.yaml),
and the existing Bazel toolchains. At this revision the JS lockfile resolves OpenAPI TypeScript
7.13.0, Hey API 0.99.0, Zod 4.6.5, json-schema-to-typescript 16.0.0, Quicktype 26.0.0,
Protobuf-ES 2.15.0, and TypeScript 5.9.3. Reuse those pins rather than introducing an alternate tool
installation for the spike.

- [OpenAPI TypeScript](https://openapi-ts.dev/) documents operation/model type generation for
  OpenAPI 3.0 and 3.1.
- [Root-export options](https://openapi-ts.dev/cli#flags)
  expose `rootTypes`, `rootTypesNoSchemaPrefix`, and `rootTypesKeepCasing`; the spike verifies their
  CLI equivalents against the repo's pinned generator.
- [Hey API Zod plugin](https://heyapi.dev/openapi-ts/plugins/zod) documents generated Zod schemas;
  the fixture checks the repo's pinned version and caller-supplied date policy.
- [Zod JSON Schema](https://zod.dev/json-schema) documents both directions. `z.fromJSONSchema` is
  experimental; the proposed adapter is intentionally bounded by tested schema features and pins.
- [`json-schema-to-typescript`](https://github.com/bcherny/json-schema-to-typescript) supplies
  model declarations, not runtime validators.
- [Protobuf-ES](https://github.com/bufbuild/protobuf-es) supplies native message schemas, bindings,
  and codecs. These stay on the protobuf path.
