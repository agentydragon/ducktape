# Test output records

The collector discovers framework-independent records in Bazel undeclared test
outputs. Each record identifies one payload from the same exact test result:

```text
$TEST_UNDECLARED_OUTPUTS_DIR/
└── test-outputs/
    ├── records/
    │   └── <key>.json
    └── files/
        └── <key>.png
```

For example, `records/<key>.json` can contain:

```json
{
  "schema": "ducktape.test-output.v1",
  "case_id": "example/test_ui.py::TestDialog::test_mobile[dark]",
  "checkpoint": "expanded",
  "role": "screenshot",
  "path": "files/<key>.png",
  "label": "Expanded dialog",
  "title": "Dialog visuals"
}
```

`case_id` and `role` are nonempty strings, and `checkpoint` is a string or
`null`. `label` and `title` are optional presentation text. The record's `path`
is a canonical relative path under `test-outputs/files/`; record and payload
must be present in the same result. Records and payload paths are independent
of the test framework. The Python writer hashes the JSON-encoded tuple
`[case_id, checkpoint, role]` for safe filenames; collectors use the fields,
not those filenames, as identity. A producer must not publish the same tuple
twice in one test result. In particular, use distinct checkpoints for multiple
captures by one case.

The `screenshot` role is a PNG and is eligible for visual review. Other roles
are collected as outputs but are not compared or uploaded as screenshot images.
The full logical identity is repository, canonical Bazel target label, case ID,
checkpoint, and role. Invocation, configuration, run, shard, attempt, status, and
CAS URI describe an observation, not the identity. A case moving to another
shard keeps its identity; different cases cannot collide by choosing the same
checkpoint.

Python tests can use the `test_outputs` pytest fixture to bind the full
repository-relative node ID automatically. `VisualPage.capture()` uses that
binding and takes an optional checkpoint. Python code outside pytest can call
`devinfra.testing.output_writer.publish_output` with an explicit case ID. Other
frameworks and languages may implement the same record format; no pytest naming
convention is required by the collector.
Use `publish_file` (or the fixture's `publish_file` method) for an existing
file; it retains the source suffix and uses the same duplicate-claim checks.

For already-materialized undeclared outputs, `local_output_result` reads a
completed output tree with caller-supplied result provenance and summary;
`collect_materialized_outputs` applies the same identity, shard, retry, and
coverage validation used for BuildBuddy results. Its source types also allow
other adapters to supply record bytes and payload references directly.
