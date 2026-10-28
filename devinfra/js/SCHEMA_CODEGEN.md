# TypeScript schema conversion

The `openapi_typescript` rule converts a declared OpenAPI JSON artifact into a TypeScript
declaration file. The schema owner produces the artifact; the converter does not invoke a backend
exporter.

```starlark
load("@aspect_rules_js//js:defs.bzl", "js_library")
load("//devinfra/js:openapi_typescript.bzl", "openapi_typescript")
load("//devinfra/js:ts_library.bzl", "ts_library")

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
    tsconfig = ":tsconfig",
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

## Standalone JSON Schema declarations

The `json_schema_typescript` rule converts one declared JSON Schema file without invoking its
producer. It has the same file-input shape as `openapi_typescript`:

```starlark
load("//devinfra/js:json_schema_typescript.bzl", "json_schema_typescript")

json_schema_typescript(
    name = "_types",
    schema = "//my/api:types_schema",
    schema_files = ["//my/api:shared_definitions"],
    out = "api/types.d.ts",
)
```

List every file reached through a relative local `$ref` in `schema_files`; the action receives each
as a declared input and resolves the reference from the root schema's path. Remote HTTP resolution
is disabled by the converter.

When a runtime consumer needs the source JSON as well as generated declarations, keep the schema
artifact as its own Bazel input and package both files in `js_library`:

```starlark
load("@bazel_skylib//rules:copy_file.bzl", "copy_file")
load("@aspect_rules_js//js:defs.bzl", "js_library")

copy_file(
    name = "_schema_json",
    src = ":types_schema",
    out = "api/types.schema.json",
)

js_library(
    name = "types",
    srcs = [":_types", ":_schema_json"],
)
```

JSON Schema carries no HTTP operation types; owners remain responsible for exporting their actual
runtime or API contract.
