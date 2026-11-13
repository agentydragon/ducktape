# Agentplane typing audit — services, shared code, and app backend

**Snapshot:** `devel` at `3531546cee7b2d8344a072288854ad13dfd2bbbc`
**Scope:** the assigned 1,231 tracked files in `agentplane/**`, excluding `env/`, `x/`, `native/`, `runner/`, `runner_transport/`, `proto/`, `app/frontend/`, `system_tests/`, and `testing/`.
**Mode:** read-only source review. No tests or checks were run and no source files were changed.

## Summary

I found two concrete display risks where a newly introduced protobuf enum value is silently presented as a known value: an unknown egress decision becomes `deny`, and an unknown sandbox phase becomes an empty string. I also found six typed-representation improvements where closed domain data is converted back to strings or integers inside the service before being stored or dispatched. Those are maintainability issues; this review did not establish an authorization or lifecycle failure from them.

The recommendations preserve external JSON strings, protobuf wire values, SQL text, Kubernetes CRD JSON, arbitrary provider payloads, and unknown numeric enum values. They introduce types inside Go and leave string/number conversion at actual protocol or storage boundaries.

## Findings

### DEV-S01 — Unknown egress outcomes are displayed as denied

**Priority:** P2 · **Confidence:** High · **Class:** Display correctness risk

`agentplane/app/go/streamapi.go:1352-1363` initializes every decision view to `"deny"` and changes it to `"allow"` only for `Decision_OUTCOME_ALLOW`. The producer contract in `agentplane/egress/egress_admin.proto:33-38` explicitly reserves `OUTCOME_UNSPECIFIED` for an outcome the schema does not name. Consequently, that value and any future enum value are currently displayed as an affirmative denial. The value feeds the live decision SSE snapshot at `streamapi.go:606`; this is an operator-facing projection, not the egress authorization decision itself.

**Recommendation:** use a named view outcome type and an exhaustive switch. Preserve `allow` and `deny`; represent unspecified/future values explicitly as `unknown` (or an equivalent stable unknown state) rather than inferring denial.

**Suggested check:** cover allow, deny, unspecified, and an unrecognized numeric enum in `decisionViews`; assert that only the explicit deny value is rendered as denied. Confirm the SSE JSON contract for the added unknown spelling with its consumer.

**Compatibility risk:** the JSON `outcome` field remains a string, but consumers that assumed only `allow`/`deny` need to handle an unknown value. No authorization behavior should change.

### DEV-S02 — Unknown sandbox phases produce an empty display value

**Priority:** P2 · **Confidence:** High · **Class:** Display correctness risk · **Overlap:** the runtime/UI worker owns the downstream display consumer; this finding is in the assigned Go backend.

`agentplane/app/go/sandboxes/views.go:112-125` produces a typed `sandboxespb.SandboxPhase`. `agentplane/app/go/sandboxapi.go:297-306` projects that enum through a map lookup. The zero enum is covered, but an unrecognized numeric value returns Go's zero string (`""`). Such a value can arrive when a newer producer adds a phase before this app's generated enum is updated, leaving the stream/API consumer with a blank phase.

**Recommendation:** make the mapping exhaustive, with a deliberate fallback such as `Unknown` or a stable rendering of the numeric value. Keep the existing human-readable labels for known phases.

**Suggested check:** test every current phase and an unrecognized numeric value through the sandbox JSON/SSE projection; ensure the unknown value is nonempty and does not alias a known lifecycle state.

**Compatibility risk:** only the previously blank unknown case changes. Confirm how the frontend handles an added phase string; no lifecycle decision should depend on this view label.

### DEV-S03 — History folding re-derives protobuf oneofs as strings for control flow

**Priority:** P2 · **Confidence:** High · **Class:** Compile-time safety / maintenance

`agentplane/history_service/threadfold/fold.go:40-49` reflects over generated protobuf descriptors and returns oneof field names as strings. `Touched` dispatches on those strings at lines 51-105, `folder.admit` converts the command `operation` oneof at lines 365-397, and `folder.fold` dispatches on the observation string at lines 471 onward. The source events and commands are already generated oneof types; `eventpb.ItemCompleted` demonstrates a typed wrapper switch in the same `Touched` code at lines 71-79.

The string representation is useful where the projection persists a canonical observation or operation name (`threadfold/types.go:145-160`, `rows.go:87-99`). It is unnecessary for selecting the Go behavior, and ties that behavior to protobuf field-name spellings without compiler help.

**Recommendation:** use type switches over `event.GetObservation().(type)` and `command.GetOperation().(type)` for business dispatch. Convert the selected variant to its canonical persisted name only at the projection/storage boundary. Avoid changing the stored name contract.

**Suggested check:** run the existing fold and `Touched` coverage after such a change; ensure each supported variant retains the same touched keys, fold result, unknown-event behavior, and persisted observation/operation strings. Add a compile-time/exhaustiveness-oriented check if the generator or project pattern supports one.

**Compatibility risk:** none intended for persisted JSON or protobuf. Refactoring must retain current unknown-oneof handling and must not infer an outcome for an unknown event.

### DEV-S04 — Protobuf item enums are stored as raw `int32` in Go row models

**Priority:** P3 · **Confidence:** High · **Class:** Typed storage model / maintenance

The in-memory history model already uses generated types (`threadfold/types.go:105-118`: `eventpb.ItemKind` and `*eventpb.RecoveryDisposition`). The Go row model erases them in `threadfold/rows.go:72-79` as `int32` and `*int32`; row construction casts at lines 224-233 and decode casts them back at lines 390-417. This duplicates conversions and permits unrelated integers to be assigned to enum fields in the storage model.

**Recommendation:** use `eventpb.ItemKind` and `*eventpb.RecoveryDisposition` in the Go stored-row struct. Both are int32-backed enum types, so ordinary `encoding/json` keeps their numeric JSON representation; typed enum fields also retain unknown numeric values instead of rejecting them. Keep the Python projection's numeric view as-is if it is part of the shared JSON contract.

**Suggested check:** round-trip known and unknown numeric values through row construction and decoding, and compare the serialized JSON shape/values byte-for-byte or structurally. Do not introduce enum-name strings into the database JSON.

**Compatibility risk:** no intended JSON/SQL migration. Verify the actual Go JSON encoder behavior and Python consumer before changing the shared row schema.

### DEV-S05 — Orchestration Action UUIDs lose their UUID type after schema validation

**Priority:** P3 · **Confidence:** High · **Class:** Identifier typing / maintenance

`agentplane/action_service/orchestration_v1/contract/inputs.go:56-114` declares eight `SessionID` fields and four `CommandID` fields as `string`, each repeating the same lowercase UUID pattern in its JSON Schema tag. `catalog.Input.Decode` validates the schema and unmarshals into these structs (`action_service/catalog/input.go:25-30, 88-95`); `sessions.go:25-100` then forwards those raw strings into protobuf `SessionId`/`CommandId`. Session IDs also flow through `reads.go:285-311` and the opaque resume-token payload in `turns.go:19-41`.

The external Action contract is correctly a JSON string and current schema validation rejects malformed IDs. The loss is after that validation: session and command IDs become interchangeable strings throughout the Go call graph, while other app/history owners already use `uuid.UUID` for session IDs.

**Recommendation:** introduce distinct `SessionID` and `CommandID` domain types backed by UUID (or a validated UUID wrapper) at the decoded Action boundary. Convert to strings only when constructing protobuf requests or JSON tokens. Keep the current lowercase UUID string schema, JSON field names, and idempotency behavior.

**Suggested check:** verify generated Action schemas are unchanged; test lowercase valid IDs and malformed IDs, and keep Session/Command domain types distinct in internal signatures; verify protobuf and resume-token JSON retain the exact canonical string representation.

**Compatibility risk:** a wrapper may need a custom `JSONSchema`/JSON implementation because reflection over a UUID array type could otherwise change the advertised schema. Do not change the external schema or normalize input differently without a contract decision.

### DEV-S06 — Closed Action policy oneof is projected into a string-tagged struct

**Priority:** P3 · **Confidence:** High · **Class:** Closed union represented as string plus optional fields

`agentplane/app/go/policyaccess/action_policy.go:98-140` describes `ActionRuleView` as a tagged policy union but represents every variant with `Type string` and optional payload fields. Its custom `MarshalJSON` switches on repeated string tags (`exact_actions`, `argument_schema`, `github_repository`, `github_public_repository`, `home_assistant_entity_control`). The producer at lines 242-273 already switches on the generated `action_servicepb.Policy` oneof and then reconstructs the string-tagged struct. The default marshaler rejects unknown strings, so I found no current incorrect-policy output path, but the internal representation permits invalid field combinations and duplicates the generated oneof vocabulary.

**Recommendation:** represent the internal result as concrete policy variant types (or a closed interface with unexported marker methods), then serialize each variant to the same JSON `type` and fields. Keep `map[string]any` only for the arbitrary JSON Schema object inside the `argument_schema` variant.

**Suggested check:** table-test each generated protobuf oneof to the corresponding JSON variant and ensure impossible mixed-field states cannot be constructed through internal constructors. Compare JSON output and generated API schema for all existing variants.

**Compatibility risk:** JSON output must remain byte/shape compatible, including empty schema objects and omission behavior. If Huma/OpenAPI reflection relies on the current DTO struct, retain a dedicated wire DTO at that boundary while using typed internal variants.

### DEV-S07 — Dispatcher state and operation strings cross the DB/protobuf boundary as untyped model values

**Priority:** P3 · **Confidence:** High · **Class:** Repeated closed vocabulary / maintenance

`agentplane/runner_gateway/dispatcher/store.go:22-32` defines several state strings, while `Dispatch.State`, `Dispatch.Operation`, `Opening.State`, and `Opening.Reason` are all plain strings at lines 59-92. `operation()` maps a generated `commandpb.Command` oneof to string values at lines 122-132. `dispatcher/snapshots.go:10-43` maps state and operation strings back to generated `DeliveryState` and `Operation` enums; some persisted states (`HARNESS_CONFIRMED`, `NO_OP`) are also inline there. The SQL columns intentionally store stable text, so using protobuf enum numeric values as the DB representation would be the wrong fix.

**Recommendation:** define local `DispatchState` and `DispatchOperation` string types with constants for the complete persisted vocabularies; use them in the in-memory DB model and return `DispatchOperation` from `operation()`. Convert to/from raw SQL strings and protobuf enums only in the storage/wire adapters. Keep `Reason` as free-form text.

**Suggested check:** use the named types through model constructors and retain validation at the SQL/wire boundaries; test representative state/operation conversions and unknown-string rejection; verify SQL text values and migrations remain unchanged.

**Compatibility risk:** none intended for SQL or gRPC. Keep the DB strings independent of protobuf enum names because the current SQL spelling is its own storage contract.

### DEV-S08 — Kubernetes UIDs in Sandbox status are typed as plain strings

**Priority:** P3 · **Confidence:** Medium-high · **Class:** Identifier typing / maintenance

`agentplane/sandboxes/api/types.go:122-158` stores incarnation, quiesce-attempt, Pod credential, and VMI identities as strings; volume bindings also use strings at lines 242-263. Producers in `agentplane/sandbox_controller/controller.go:566,659`, `idle.go:137-139`, `runner_credential.go:99-109,244-301`, and `state_volume.go:79` repeatedly convert `pod.UID`, `vmi.UID`, or `pvc.UID` from Kubernetes' `types.UID` to string. Consumers compare those fields back to `string(object.UID)`, including the incarnation check in `controller.go:128-129`. The project already uses `types.UID` for Kubernetes identities in `agentplane/policy/api/v1alpha1/types.go:95` and `action_service/orchestration_v1/contract/inputs.go:31-34`.

**Recommendation:** keep Kubernetes object UIDs as `types.UID` through CRD API models and controller records; convert at annotation, SQL, protobuf, or other explicitly string-only boundaries. Preserve the JSON property names and string representation.

**Suggested check:** regenerate CRD schemas and verify every field remains `type: string` with the same optionality; compare serialized status JSON; cover stale/replaced Pod, VMI, PVC, and quiesce-attempt UID comparisons. Confirm code-generation/deep-copy support for the alias before broad adoption.

**Compatibility risk:** changing Go field types should not change CRD JSON because `types.UID` is string-backed, but generated schema and deepcopy output must be checked. Do not add a UID format constraint: Kubernetes UIDs are opaque identifiers, and existing test values need not be UUIDs.

## Accepted boundary uses and non-findings

- `action_service/catalog.Input` validates external JSON against a reflected schema before decoding. Keeping JSON inputs as strings at the wire/schema boundary is correct; DEV-S05 concerns the post-validation Go representation.
- `agentplane/action_service/policies` accepts dynamic external MCP tool arguments. Policy evaluation intentionally treats unknown keys/types as unapproved; this is an open external JSON boundary, not a closed internal domain model.
- PolicySet rule `map[string]any` values and `app/go/policyaccess/views.go:35` preserve arbitrary future rule fields. Do not replace them with a closed schema that drops unknown data. DEV-S06 is limited to the separately closed Action policy oneof.
- History native Claude/Codex record maps and selective decoding in `history_service/storage` preserve evolving provider payloads, explicit nulls, and numeric/wire details. They are not candidates for blanket `map[string]any` removal.
- `sandboxespb` Kubernetes Pod `Struct` status in `app/go/sandboxes/views.go:101-108`, runner/protobuf JSON views, and `threadcommands.protoJSONMap` are serialization boundaries. Dynamic maps there are pass-through JSON payloads; no business or authorization decision is driven by their dynamic Go types.
- `capture` framework request/response values, SQL driver `any`, SQLAlchemy extension hooks, settings/config maps, and Kubernetes unstructured/metadata objects are likewise boundary or extension types.
- `notification_service` legacy Action state codec and Action Service storage codecs deliberately map protobuf enums to documented database/legacy spellings; they validate unknown values and preserve a storage contract. They should not be replaced by protobuf enum `.String()` without a storage migration.
- `mcp_edge` enum parsing checks unknown values; CLI state/event mapping rejects unsupported enum values rather than silently defaulting. These are not findings.
- Go view fields that are strings solely to retain an external JSON/API spelling are not automatically defects. The two enum projection findings above are retained because the current fallback changes unknown values into a known state or an empty value.

## Coverage ledger and search method

The parent-provided inventory `/tmp/agentplane-pr-refresh/typing-audit/services-files.txt` contains 1,231 tracked files. Extension counts: 715 Go, 156 Python, 13 `.proto` files outside the separately excluded `agentplane/proto/` tree, 18 SQL, 167 Markdown, 133 Bazel, 11 JSON, 7 YAML, 4 Nix, 3 text, 3 JavaScript, and 1 Jinja template. Counts sum to 1,231.

| Assigned component family       | Coverage                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Go services and libraries       | `action_service`, `app/go` backend, `capture`, `cli`, `egress`, `grpc`, `history_service`, `indexing`, `llm_ingress`, `mcp_edge`, `notification_service`, `observability`, `policy`, `policy_controller`, `postgres`, `runner_gateway`, `sandbox_controller`, `sandboxes`, `settings`, `watchhealth`, `workload_auth`, plus top-level Go sources and their tracked tests/build files |
| Python services and models      | Python under `action_service`, `app`, `capture`, `history_service`, `indexing`, `mcp_edge`, `notification_service`, `policy`, `policy_controller`, `sandboxes`, and top-level `crd_group.py`, `log_levels.py`, `policy_sets.py`, `sandbox_resource.py`, `subjects.py`, plus tracked Python tests/build files                                                                         |
| Schema and persistence          | The 13 assigned `.proto` sources outside the excluded `proto/` prefix and all 18 SQL files/migrations; generated/excluded `agentplane/proto/` definitions were consulted only as cross-scope type owners where needed                                                                                                                                                                |
| Supporting languages and config | 3 JavaScript testing/inspection helpers, 1 Jinja config template, 4 Nix files, 7 YAML, 11 JSON, 133 Bazel files, 3 text files, Markdown and remaining top-level project files; no production TypeScript is in this assignment                                                                                                                                                        |
| Excluded/overlap scope          | `env/`, `x/`, `native/`, `runner/`, `runner_transport/`, `proto/`, `app/frontend/`, `system_tests/`, and `testing/` belong to the other runtime/UI scan. UI payload consumption is mentioned only where relevant to DEV-S01/02/06. PR #13428 is a separate audit.                                                                                                                    |

I read root `AGENTS.md`, `README.md`, and `STYLE.md`, plus `agentplane/AGENTS.md` and `agentplane/app/AGENTS.md`. The relevant local guidance is to use UUIDs through Go layers and convert at boundaries, model invalid-state-sensitive variants as typed unions, and keep absent values distinct from empty values.

Candidate search used a broad `rg` pass over the complete tracked inventory for `map[string]any`/`interface{}`, `any`, string-typed `Kind`/`Type`/`State`/`Outcome`/`Operation`/`UID`/`ID` fields, `oneofName`, and `UNSPECIFIED`. It yielded 1,967 textual matches in 491 files (including tests, docs, build/config, generated references, and intentional external payloads). Follow-up searches targeted enum fallbacks, protobuf oneof dispatch, UUID/UID fields, Go/Python dynamic maps, and string↔enum storage/protocol conversions. Each retained finding above was traced from its producer through its consumer and checked against the relevant schema/storage boundary. The accepted-boundary section records the main false-positive classes. No candidate count is presented as a defect count.
