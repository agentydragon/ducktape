# Pydantic schema registration spike

`TypeAdapter.json_schemas` is a useful allocator for one batch of related model roots. Against the pinned Pydantic 2.13.5, this spike exercises shared nested definitions, two distinct classes with the same name, validation and serialization modes, nested local refs, and compatibility with FastAPI's `#/components/schemas/` ref space. A generated TypeScript consumer imports the resulting model types through the existing `json_schema_js_library` and `ts_library` rules.

Pydantic returns each named root as a reference when `ref_template` names that root, and returns its schema body in the definitions mapping. Merge those definition bodies into FastAPI's component table; do not insert the root reference at its own key, which would create a self-reference. Add a root alias only when a distinct component key is needed. The fixture validates a route schema and an added root against the combined component document, then shows that merging an incompatible body at an existing component key changes the schema followed by FastAPI's existing route reference. The allocator resolves collisions only among adapters in that call and has no input for FastAPI's already populated components. A production migration must check generated definition and root names against the existing component table before inserting them, and fail clearly when a same-name schema is incompatible. This spike does not introduce that production check and does not demonstrate a collision in current production schemas.

Future registration should use one `TypeAdapter.json_schemas` call for the standalone models owned by each document, with explicit `(schema key, mode, TypeAdapter)` entries and a ref template matching that document. Preserve the current modes and payload boundaries:

| Owner                                                           | Models added outside FastAPI routes                                     | Mode          | Contract to retain                                                                  |
| --------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------- |
| [Agentplane exporter](../../../agentplane/app/export_schema.py) | [`ThreadEntityView`](../../../agentplane/history_service/fold/views.py) | validation    | Electric's streamed entity rows and their discriminated `entity_kind` union         |
| [Augur exporter](../../../finance/augur/api/export_schema.py)   | [`BudgetSummaryCsvRequest`](../../../finance/budget/wire.py)            | validation    | The undocumented CSV request, including its tagged window and adjustment values     |
| [Study Casino exporter](../../../study_casino/export_schema.py) | [`WsStateChangedMessage`](../../../study_casino/state.py)               | validation    | The `/ws` state-change frame, which FastAPI does not document as a route model      |
| [Airlock exporter](../../../airlock/export_schema.py)           | [The five exported models](../../../airlock/models.py)                  | serialization | The existing `AirlockTypes` JSON Schema envelope describes values sent to consumers |

For the three FastAPI owners, leave route-derived schemas under FastAPI's existing component names and register their extra models alongside them after the explicit collision check. Airlock has no FastAPI component table; its models can share one serialization-mode allocation and remain properties of the existing wrapper document. The runnable tests use the real Agentplane stream-row model, Augur CSV request model, Study Casino WebSocket model, and all five Airlock export models. The Agentplane and Augur tests live under their owning `x/` packages so they can depend on owner-scoped Bazel targets without widening visibility. The generated schemas are checked by the independent `jsonschema` Draft 2020-12 validator. No exporter is changed and schema audit C remains open.

Run the spike with:

```bash
bbr test //devinfra/x/pydantic_schema_registration:registration_test
bbr test //agentplane/x/pydantic_schema_registration:test_thread_entity_schema
bbr test //finance/x/pydantic_schema_registration:test_csv_request_schema
bbr build //devinfra/x/pydantic_schema_registration:consumer
```
