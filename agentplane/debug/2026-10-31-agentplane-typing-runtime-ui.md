# Agentplane typing audit: runtime and UI

**Snapshot:** `3531546cee7b2d8344a072288854ad13dfd2bbbc` (devel)
**Scope:** tracked files in `agentplane/{env,x,native,runner,runner_transport,proto,system_tests,testing}/` and `agentplane/app/frontend/`.
**Method:** complete tracked-file inventory, language/type-pattern searches, then manual tracing of retained candidates through producers and consumers. This is a source audit, not runtime proof. No tests were run.

## Findings

No P1 issue or demonstrated runtime incident was found. The candidates below are P2 representation and contract improvements: existing validation generally contains current values, but domain facts are represented as strings or inferred from open maps at important boundaries.

### DEV-R01 — Supervision mode is a string across the Go runtime

**Priority:** P2 · **Confidence:** High

The runner configuration already defines the public JSON contract as a Pydantic tagged union: `PodEnvironment.kind` / `supervision` are `Literal["pod"]` and `Literal["pid1", "subprocess"]`; VM has `Literal["vm"]` ([agentplane/runner/configuration.py:207-224](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/runner/configuration.py#L207)). Go decodes both tags into strings and carries supervision as a string through resolution and runtime setup: [configuration.go:185-188,209-240](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/configuration/configuration.go#L185), [resolved.go:62-72](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/configuration/resolved.go#L62), [runtime.go:64-71,206-218,225-268](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/runtime/runtime.go#L64). The same values select service setup and socket layout at [execution_service.go:144-181](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/runtime/execution_service.go#L144).

**Producer → consumer:** Runner emits the strict JSON environment union → Go `resolveEnvironment` validates strings → `configuration.Resolved` and `runtime.Config` retain a free-form string → Runtime branches on those values.

**Canonical owner and remedy:** The runner Pydantic model owns wire values. Define a Go `SupervisionMode` with the three supported values at the Go config boundary, decode/validate into it once, and thread it through `Resolved` and `runtime.Config`. Keep the same JSON spelling and checks that bind PID1/subprocess to Pod and systemd to VM. This is a local representation improvement, not a second wire schema.

**Risk:** No JSON or storage change if the named Go type keeps the same underlying string and serialization. Preserve invalid-value rejection and the current environment/launcher invariants.

**Suggested checks (not run):** table-driven decode tests for the three valid modes and unknown values; runtime invariant tests for PID1, subprocess, and systemd; VM/Pod config fixtures.

### DEV-R02 — Execution backend is an untyped string in its persisted config

**Priority:** P2 · **Confidence:** High

The standalone service config exposes `Backend string` alongside backend-specific optional fields ([agentplane/env/execution/service/config.go:18-32](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/service/config.go#L18)). `Validate` accepts `pod`, `companion`, and `systemd` and checks combinations of `Systemd`, binary, working directory, and container socket ([config.go:60-94](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/service/config.go#L60)). The runtime produces `systemd` or `companion` strings ([agentplane/env/runtime/execution_service.go:151-169](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/runtime/execution_service.go#L151)); the service consumes them in a string switch ([agentplane/env/execution/service/service.go:46-65](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/service/service.go#L46)).

**Producer → consumer:** Runtime chooses a backend and writes `execution.json` → `service.Load` unmarshals an arbitrary string → validation and service dispatch branch on it.

**Canonical owner and remedy:** `service.Config` owns this internal config contract. Introduce a named Go `Backend` type and constants for the three validated values, use them at production and dispatch sites, and keep validation for values decoded from JSON. A later variant-struct redesign could make backend-specific field combinations unrepresentable, but is broader than this candidate.

**Risk:** Keep existing JSON keys and string values to avoid config-file changes. The named type alone does not validate JSON input; `Validate` must continue rejecting unknown values and bad field combinations.

**Suggested checks (not run):** one valid config per backend, unknown backend, and each invalid optional-field combination; round-trip the existing JSON config shape.

### DEV-R03 — Companion control operation is a free-form string

**Priority:** P2 · **Confidence:** High

The local Unix-socket request defines `Operation string` ([agentplane/env/execution/companion/control/control.go:12-16](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/companion/control/control.go#L12)). The companion compares it against `claim`, `inspect`, and `fence`, with unknown operations falling into connection-close behavior ([agentplane/env/execution/companion/main.go:166-173,217-249](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/companion/main.go#L166)). Callers construct those strings in the health handler and executor server ([main.go:75-85](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/companion/main.go#L75), [server.go:239-263](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/codexexecserver/server.go#L239)).

**Producer → consumer:** Runner-side health / execution code writes a JSON request → companion decodes a string and branches on its spelling.

**Canonical owner and remedy:** The `control` package owns this local protocol. Define a named `Operation` type with constants and centralize decoding/validation so an unsupported operation returns a clear protocol error instead of looking like a dropped connection. Keep `Environment map[string]string` open because its keys are caller-provided process environment variables.

**Risk:** Preserve the same JSON strings and the lease/fence behavior. The environment map and error text are payload data, not enum candidates.

**Suggested checks (not run):** JSON round-trip for all three operations; unsupported and missing operation rejection; ensure claim keeps the connection as the lease and fence still requests process-domain shutdown.

### DEV-R04 — Process-domain generation is UUID identity stored as string

**Priority:** P2 · **Confidence:** High

The companion creates each process-domain generation with `uuid.NewString()` on startup and after a completed fence ([agentplane/env/execution/companion/main.go:148-150,320-333](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/companion/main.go#L148)). The control response stores it as a string and only checks non-emptiness after decoding ([control.go:18-25,42-50](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/companion/control/control.go#L18)). The executor server forwards and compares it as an unvalidated string while waiting for a fresh generation ([agentplane/env/execution/codexexecserver/server.go:239-258,261-280](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/codexexecserver/server.go#L239)).

**Producer → consumer:** Companion creates a UUID generation → JSON status → caller captures it → stop/fence compares the returned generation to the captured token.

**Canonical owner and remedy:** The companion control protocol owns this identity. Use the existing UUID package's `uuid.UUID` type for `Status.Generation` and `*uuid.UUID` for the optional `FenceContainer` prior generation; represent the initial absence with nil. This makes malformed identities fail during decoding instead of passing a nonempty-string check.

**Risk:** UUID JSON remains a string, so valid wire values do not change. Ensure nil retains the initial fence behavior and that all existing fixture generations are valid UUIDs. Do not generalize unrelated externally supplied IDs to UUID without evidence.

**Suggested checks (not run):** valid generation JSON round-trip; malformed and missing generation rejection; first-start absent prior generation; changed generation after fence; stale generation continues waiting until fence evidence arrives.

### DEV-R05 — Signal RPC admits arbitrary strings although the adapter supports only interrupt

**Priority:** P2 · **Confidence:** High

The executor protobuf contract declares `SignalProcessRequest.signal` as `string` ([agentplane/env/execution/executor.proto:119-125](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/executor.proto#L119)). Current Agentplane dispatch constructs `interrupt` ([agentplane/x/central_sampler/executor.go:548-551](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/x/central_sampler/executor.go#L548)), while the adapter rejects any other value and passes the string to the native `process/signal` JSON-RPC request ([agentplane/env/execution/adapter/methods.go:99-109](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/execution/adapter/methods.go#L99)). The intermediate executor operation also stores the signal as a string ([agentplane/x/central_sampler/executor_operations.go:855-857](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/x/central_sampler/executor_operations.go#L855)).

**Producer → consumer:** Central sampler selects a signal → generated gRPC request exposes any string → adapter checks the one supported value → native Codex protocol receives that string.

**Canonical owner and remedy:** The executor RPC contract should state the supported operation. Represent it as an enum at the Agentplane boundary and map the known interrupt value to the upstream string in the adapter. Do not change protobuf field 5 from string to enum in place: protobuf string and enum use different wire types. If mixed-version RPC peers are possible, add a versioned enum field or RPC and retain legacy handling through a deliberate transition; if peers are upgraded atomically, regenerate the contract and update all callers together. Keep the upstream JSON-RPC field string-valued at the external boundary.

**Risk:** This is the only finding here with a possible protobuf wire compatibility cost. No SQL change is needed. Unknown enum/legacy values must be rejected explicitly; do not silently map them to interrupt.

**Suggested checks (not run):** known interrupt maps to exactly `"interrupt"`; unspecified, unknown, and legacy unsupported values reject before native dispatch; verify generated Go/Python clients and any mixed-version behavior for the selected migration path.

### DEV-R06 — Tool item completion is inferred from an opaque result map with unchecked assertion/default

**Priority:** P2 · **Confidence:** High for the representation gap; no malformed production input was demonstrated.

`FinishTool` already receives typed call and output arguments, but its semantic result remains a `map[string]any` ([agentplane/x/central_sampler/store.go:1427-1430](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/x/central_sampler/store.go#L1427)). The shared evidence writer persists the supplied payload, then decides whether to append `ItemCompleted` by inspecting map keys. It silently converts a missing or wrongly typed `call_id` to an empty ID and defaults `succeeded` to true unless `success` is a boolean ([agentplane/x/central_sampler/evidence.go:96-110](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/x/central_sampler/evidence.go#L96)). Runtime producers currently build the expected map, including explicit failure cases, and omit `success` on success ([runtime.go:900-910,922-945](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/x/central_sampler/runtime.go#L900)).

**Producer → consumer:** Runtime creates semantic call/output/success facts plus a result payload → Store persists the payload as intent result and boundary evidence → evidence projection re-reads that payload map to construct a typed completion event.

**Canonical owner and remedy:** `FinishTool` owns the semantic completion boundary. Pass a typed completion value (call ID from the typed invocation, output, and explicit success) to the projection path, separate from the raw audit/result payload. Keep the raw map and its JSON evidence unchanged and open; do not replace provider/tool result maps with a closed schema. Treat malformed semantic IDs as impossible by construction or reject them before emitting the event.

**Risk:** No storage schema or raw evidence change is needed. Valid current success behavior must remain success even though its raw map omits `success`; failed and unsupported calls must remain failures. This change must not collapse durable intent state, tool completion, model continuation, or request admission into one outcome, and it must not trigger replay of an uncertain operation.

**Suggested checks (not run):** normal success with omitted payload `success`; explicit failure; malformed/missing semantic ID cannot create an empty-ID event; raw result JSON remains byte/value equivalent; unknown external provider payload remains preserved.

### DEV-R07 — Thread state TS type and Zod validator are duplicate schemas

**Priority:** P2 · **Confidence:** High

`threads/client.ts` manually declares the `ThreadEntityState` union ([agentplane/app/frontend/threads/client.ts:30-60](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/app/frontend/threads/client.ts#L30)). `threads/electric_sync.tsx` separately encodes the same variant shapes and nullability in `stateSchema`, uses it in `entitySchema`, and parses rows through that schema ([electric_sync.tsx:58-105,734-745,782-798](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/app/frontend/threads/electric_sync.tsx#L58)). The duplication can drift while only runtime parsing catches the mismatch.

**Producer → consumer:** The thread fold constructs the row-state variants in `agentplane/history_service/threadfold/rows.go:52-99` (outside this assignment; owned by the services reviewer) → Electric reader validates via Zod → client and rendering code compile against a separately maintained TypeScript union.

**Canonical owner and remedy:** Keep one frontend schema owner, preferably a shared thread-entity schema module. Export `ThreadEntityState = z.infer<typeof stateSchema>` and use that type in `client.ts`, while `electric_sync.tsx` uses the same schema for parsing. Preserve current validation semantics and nullable fields. Keep `event: z.unknown()` open because it contains archived observation payloads whose variants can evolve independently.

**Risk:** No backend wire or storage change is needed. Do not narrow cursors to JS numbers: the current `Decimal = string | bigint` / `decimalBigInt` path preserves values above 2^53 ([thread_sync.ts:11-14](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/app/frontend/threads/thread_sync.ts#L11)); the `as unknown as number` in `client.ts` is documented as a type-only workaround for a serializer that does not convert the path value to a number ([client.ts:65-74](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/app/frontend/threads/client.ts#L65)).

**Suggested checks (not run):** TypeScript typecheck; parse representative row variants with nullable fields; a test demonstrating the inferred type and runtime schema share the same union; preserve unknown observation events and large decimal cursors.

## Coverage ledger

The parent inventory contains **966 tracked files** in this assignment. Its union with the services assignment is exactly all 2,197 tracked Agentplane files, with no unassigned path. The language inventory below counts tracked code/contract files by suffix; it is not a claim that each line was manually read.

| Component           | Tracked files |      Go |  Python |      TS |     TSX |  Proto |   SQL |      JS/MJS | Review notes                                                              |
| ------------------- | ------------: | ------: | ------: | ------: | ------: | -----: | ----: | ----------: | ------------------------------------------------------------------------- |
| `env/`              |           231 |     172 |       3 |       — |       — |      2 |     2 |           — | Runtime, configuration, executor boundary, supervision, harness protocols |
| `x/`                |           121 |      80 |      15 |       — |       — |      1 |     2 |           — | Sampler, tool/executor dispatch, evidence persistence                     |
| `native/`           |            81 |       — |      68 |       — |       — |      — |     — |           — | Provider-native blocks and protocol adapters                              |
| `runner/`           |            42 |       1 |      26 |       — |       — |      1 |     — |           — | Runner config and tests; strict Pydantic config inspected                 |
| `runner_transport/` |             8 |       5 |       1 |       — |       — |      — |     — |           — | Security mode enum and transport credentials                              |
| `proto/`            |            11 |       — |       1 |       — |       — |      7 |     — |           — | Generated types/contracts and supporting code                             |
| `system_tests/`     |           144 |       — |      86 |       — |       — |      — |     2 |           — | Kubernetes-facing test scenarios and SQL fixtures                         |
| `testing/`          |            23 |       8 |       9 |       — |       — |      — |     — |           — | Test helpers and fixtures                                                 |
| `app/frontend/`     |           305 |       — |      14 |     118 |     120 |      — |     — | 1 JS, 1 MJS | UI schemas, API client, protocol generator                                |
| **Total**           |       **966** | **266** | **223** | **118** | **120** | **11** | **6** |       **2** | **746 code/contract files; 220 other tracked files**                      |

The code/contract inventory totals **152,498 lines**. The remaining tracked files include BUILD/Bazel and Starlark, Nix, service/configuration files, templates, documentation, and generated or fixture assets. They were accounted for in the path inventory; source-like configuration was searched for domain discriminators. The frontend `protocol/generate.mjs` is generator glue for upstream Codex protocol types, not a separate domain owner. BUILD/Bzl hits were target and package names; `platform_instructions.j2` is prose. The relevant Nix/service supervision values align with the supervision candidate in DEV-R01.

### Candidate searches and classification

These are broad heuristic matches, not manual-review counts. Queries overlap. The retained findings above were manually traced; a regex match alone was not treated as a finding.

| Search family                                                                                                            |     Candidate matches | Classification                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------ | --------------------: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Go domain-looking string fields (`State/Status/Outcome/Result/Kind/Type/Phase/Mode/Decision/Lifecycle/Operation string`) |   52 lines / 22 files | Retained only where the string is a closed internal domain value (R01-R04); excluded externally supplied text, dynamic protocol data, descriptions, and diagnostics.                                                                   |
| Go `map[string]any`, `[]any`, and assertions                                                                             |  354 lines / 43 files | Retained R06 where a generic result controls a typed completion event. Excluded raw provider inputs/outputs, audit evidence, arbitrary JSON objects, and boundary records.                                                             |
| Python `Any`, generic dict/Mapping, `Literal`, and casts                                                                 | 555 lines / 111 files | Validated provider/config boundaries and test helpers; retained no further internal state candidate. Native Claude uses discriminated blocks and allows unknown external fields intentionally.                                         |
| Frontend `any`, casts, open records, unknown Zod fields, and string state declarations                                   |  153 lines / 50 files | Retained duplicate state schema R07. Excluded API boundary strings that preserve newer enum members, `event: unknown`, and decimal string/bigint cursors.                                                                              |
| Proto enum/string/oneof candidate search                                                                                 |   170 lines / 9 files | Retained R05 for the signal RPC. Existing lifecycle/outcome messages already use enums/oneofs; event boundary descriptors remain strings because they label raw observations and internal progress with different extensibility needs. |

The most relevant inspected exclusions and non-findings:

- `runner_transport.SecurityMode` already has named constants and rejects unknown values ([security.go:15-25](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/runner_transport/security.go#L15)).
- Runner environment configuration is already a strict tagged Pydantic union with literal values ([runner/configuration.py:207-224](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/runner/configuration.py#L207)).
- Codex adapter decodes known generated variants and retains unknown item JSON as raw evidence; tests cover a future tool and a >2^53 integer ([agentplane/env/harnesshost/codex/adapter.go:793-835](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/harnesshost/codex/adapter.go#L793), [adapter_test.go:43-64](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/env/harnesshost/codex/adapter_test.go#L43)). Keep this open fallback and its exact raw bytes.
- Claude native blocks use a discriminated union with an unknown block fallback; tool input dictionaries remain open for provider-defined schemas ([agentplane/native/claude/blocks.py](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/native/claude/blocks.py)).
- Provider requests/results, sampler boundary payloads, native frames, and archived observations are intentionally open. In particular, `BoundaryRecord.boundary/direction` and `ExecutionProgress.kind/phase` label recorded data whose producer vocabularies differ; narrowing them globally risks losing raw or future variants ([agentplane/proto/event.proto:216-239](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/proto/event.proto#L216)). Executor `process/read` stream names likewise originate in the upstream JSON-RPC protocol and should remain string-valued at that boundary.
- System test Kubernetes dictionaries model Kubernetes API objects and diagnostics; they are not internal Agentplane state schemas.
- Outcome boundaries remain distinct: command submission/refusal/unconfirmed delivery, executor completed/rejected/unknown outcome, durable intent result, and model continuation each have separate representations. Existing unknown-outcome paths are explicit and avoid automatic retry. Do not merge these vocabularies or infer that an uncertain effect is safe to replay.
- Existing protobuf `HarnessState`, setup/launch/process states, `TurnStatus`, `ItemKind`, `RecoveryDisposition`, and executor outcome enums already own their domain values. Frontend turn labeling explicitly returns `unrecognised` for newer generated enum names rather than treating them as errors ([history_rows.ts:135-153](https://github.com/agentydragon/ducktape/blob/3531546cee7b2d8344a072288854ad13dfd2bbbc/agentplane/app/frontend/threads/history_rows.ts#L135)).

## Completeness and limits

The full assigned tracked-path inventory and language distribution were checked. Broad searches covered likely enum-like fields, generic data structures, frontend runtime schemas, and protobuf string fields; follow-up source tracing verified each retained producer-consumer path and the high-risk open-boundary exclusions above. The report does **not** claim that every line returned by heuristic searches, every generated line, or all 152,498 code/contract lines received independent manual review. There was no implementation, test execution, runtime validation, or cluster action.
