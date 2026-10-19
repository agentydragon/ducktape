# Retired Haku Console tool renderers

Source-only snapshots of the enriched Gmail, Calendar, Grocy, Haku Routine, and Tana tool-call
renderers that were removed from the shipped console. Every code file ends in `.txt`, and this
directory has no `BUILD.bazel` or tests, so Bazel does not compile or package it.

`frontend/tool_rendering/` contains the renderer modules. Adjacent files under `frontend/` are the
renderer's former client, card, schema-validation, formatting, and display helpers. `schemas/`
holds the generated MCP argument and result JSON Schemas captured before the exporter was removed.
`contracts/tool_calls.py.txt` is the matching shared wire-model snapshot, including the former
`ToolCallRecord` fields. `frontend/styles.src.css.txt` preserves the former renderer and shell CSS.

To reuse a renderer, copy the selected `.ts.txt` or `.tsx.txt` source back to its original frontend
path and rebuild its Bazel targets around the current API. The snapshots depend on the archived
wire model and generated schemas; they also use React, Mantine, Zod, `date-fns`, the MCP TypeScript
SDK, the Haku Console bridge package, Tabler icons, and build-generated OpenAPI and brand-icon
modules. Those runtime and generated dependencies must be available or adapted when restoring it.
