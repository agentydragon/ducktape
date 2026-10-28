# OpenAPI TypeScript declarations

The `openapi_typescript` rule converts a declared OpenAPI JSON artifact into a TypeScript declaration
file. The schema owner produces the artifact; this rule does not invoke a backend exporter.

```starlark
load("//devinfra/js:openapi_typescript.bzl", "openapi_typescript")

openapi_typescript(
    name = "_schema_types",
    schema = "//my/api:openapi_schema",
    out = "api/schema.d.ts",
)
```

`schema` and every entry in `schema_files` must be a single-file Bazel target. List each file reached
through a relative local `$ref` in `schema_files`; the conversion action receives the files through
declared Bazel inputs and `$(location)` paths. Source, generated, and cross-package targets are
supported. Wrap the generated declaration in a `js_library` when consumers need it as a dependency.

The generated declarations preserve `openapi-typescript`'s `rootTypes: true` and
`rootTypesNoSchemaPrefix: true` options so component models keep their named imports. Remote
references are disabled during generation: the launcher temporarily replaces `fetch` and restores it
after conversion. A loopback-server test verifies that a remote `$ref` is rejected without an HTTP
request, while a local document with an absolute `$id` still converts.

Schema exporters remain responsible for their source contract and any serialization choices. This
rule does not normalize schema dialects or merge OpenAPI and JSON Schema inputs.

Reference interpretation otherwise follows the pinned OpenAPI parser. In particular, it may treat a
literal `$ref` key nested inside example or default data as a schema reference; payloads with that
shape are not claimed as supported by this conversion path.
