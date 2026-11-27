# TypeScript schema conversion

The `openapi_js_library` macro converts a declared OpenAPI JSON artifact into a TypeScript
declaration file and packages it in a `js_library` under the requested target name. The schema owner
produces the artifact; the converter does not invoke a backend exporter.

```starlark
load("//devinfra/js:openapi_js_library.bzl", "openapi_js_library")
load("//devinfra/js:ts_library.bzl", "ts_library")
load("//devinfra/python:defs.bzl", "py_module_stdout")

py_module_stdout(
    name = "openapi_schema",
    module = "export_schema",
    out = "openapi.json",
)

openapi_js_library(
    name = "schema",
    schema = ":openapi_schema",
)

ts_library(
    name = "client",
    srcs = ["client.ts"],
    tsconfig = ":tsconfig",
    deps = [":schema"],
)
```

`py_module_stdout` runs the producer package’s `export_schema` Python module and captures its stdout.
The module’s `py_library` remains managed by Gazelle.

`schema` and every entry in `schema_files` must be a single-file Bazel target. List each file
reached through a relative local `$ref` in `schema_files`; source, generated, and cross-package
targets are supported. The generated declarations preserve `rootTypes: true` and
`rootTypesNoSchemaPrefix: true` so component models keep their named imports.

The requested `schema` target publishes the generated declaration through `JsInfo.transitive_types`
without running a compiler action. The raw file defaults to `:<name>.d.ts` (`:schema.d.ts` here); set
`out` to choose another path. Both targets inherit the macro call's `visibility`, so packages
allowed to consume the library can also address the output file directly. `ts_library` consumes the
declaration for type checking and does not emit it as JavaScript source.

## OpenAPI Zod source

`openapi_zod_ts_library` generates and compiles Zod validators from a declared OpenAPI document.
Supply the consuming package's tsconfig and declare local schema references in `schema_files`.
The generator selects the Hey API Zod plugin, and the macro supplies the generated library’s Zod dependency.

```starlark
load("//devinfra/js:openapi_zod_ts_library.bzl", "openapi_zod_ts_library")

openapi_zod_ts_library(
    name = "schema_zod",
    schema = ":openapi_schema",
    tsconfig = ":tsconfig",
    schema_files = [":referenced_schema"],
)
```

The raw generated source is also available by file label, `:schema_zod.ts` by default. JavaScript
consumers import the compiled `schema_zod.js`; TypeScript consumers resolve `schema_zod.d.ts`.
Set `out` to choose another source path. An `.mts` output selects `.mjs` and `.d.mts` when an
integration specifically requires those extensions.

Both OpenAPI launchers use the shared remote-reference guard. They fail closed if a generator
attempts HTTP resolution during the build. The repository-wide map of schema mechanisms and when to
pick each is <../docs/schema_codegen.md>.

Remote references are disabled during generation. Schema exporters remain responsible for their
source contract and serialization choices; this rule does not normalize schema dialects or merge
OpenAPI and JSON Schema inputs. Reference interpretation otherwise follows the pinned OpenAPI
parser. In particular, it may treat a literal `$ref` key nested inside example or default data as a
schema reference; such payloads are not claimed as supported by this conversion path.

## Standalone JSON Schema declarations

The `json_schema_js_library` macro converts one declared JSON Schema file without invoking its
producer. Its named `js_library` publishes the original schema, declared local-reference files, and
generated declaration together through `JsInfo`. It has the same file-input shape as
`openapi_js_library`:

```starlark
load("//devinfra/js:json_schema_js_library.bzl", "json_schema_js_library")

json_schema_js_library(
    name = "types",
    schema = "//my/api:types_schema",
    schema_files = ["//my/api:shared_definitions"],
)
```

List every file reached through a relative local `$ref` in `schema_files`; the action receives each
as a declared input and resolves the reference from the root schema's path. The generated file
defaults to `:<name>.d.ts` (`:types.d.ts` here); set `out` when consumers need another module path.
Remote HTTP resolution is disabled by the converter. The named library exposes the schema and
`schema_files` at their producer-owned paths, so runtime consumers can import those JSON modules
directly alongside the generated declarations.

Declare the macro beside the schema producer; downstream consumers depend on that target and
import the original JSON and generated types at their natural paths:

```starlark
load("//devinfra/js:json_schema_js_library.bzl", "json_schema_js_library")

json_schema_js_library(
    name = "tool_arguments",
    schema = ":types_schema",
    schema_files = [":shared_definitions"],
)
```

JSON Schema carries no HTTP operation types; owners remain responsible for exporting their actual
runtime or API contract.
