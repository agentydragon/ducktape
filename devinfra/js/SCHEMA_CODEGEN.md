# OpenAPI TypeScript declarations

The `openapi_typescript` rule converts a declared OpenAPI JSON artifact into a TypeScript
declaration file. The schema owner produces the artifact; the converter does not invoke a backend
exporter.

```starlark
genrule(
    name = "openapi_schema",
    outs = ["openapi.json"],
    cmd = "$(location :export_schema_bin) > $@",
    tools = [":export_schema_bin"],
)

openapi_typescript(
    name = "_schema_types",
    schema = ":openapi_schema",
    out = "api/schema.d.ts",
)

js_library(
    name = "schema",
    srcs = [":_schema_types"],
)

ts_library(
    name = "client",
    srcs = ["client.ts"],
    deps = [":schema"],
)
```

`schema` and every entry in `schema_files` must be a single-file Bazel target. List each file
reached through a relative local `$ref` in `schema_files`; source, generated, and cross-package
targets are supported. The generated declarations preserve `rootTypes: true` and
`rootTypesNoSchemaPrefix: true` so component models keep their named imports.

`js_library` publishes the generated declaration through `JsInfo.transitive_types`; it does not
run a compiler action. `ts_library` consumes those declarations for type checking and does not emit
them as JavaScript source.

Remote references are disabled during generation. Schema exporters remain responsible for their
source contract and serialization choices; this rule does not normalize schema dialects or merge
OpenAPI and JSON Schema inputs. Reference interpretation otherwise follows the pinned OpenAPI
parser. In particular, it may treat a literal `$ref` key nested inside example or default data as a
schema reference; such payloads are not claimed as supported by this conversion path.
