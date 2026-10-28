# TypeScript schema conversion

The `openapi_js_library` macro converts a declared OpenAPI JSON artifact into a TypeScript
declaration file and packages it in a `js_library` under the requested target name. The schema owner
produces the artifact; the converter does not invoke a backend exporter.

```starlark
load("//devinfra/js:openapi_js_library.bzl", "openapi_js_library")
load("//devinfra/js:ts_library.bzl", "ts_library")

genrule(
    name = "openapi_schema",
    outs = ["openapi.json"],
    cmd = "$(location :export_schema_bin) > $@",
    tools = [":export_schema_bin"],
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

`openapi_zod_ts_library` generates Zod TypeScript using a local Hey API config and compiles it as a
`ts_library`. The config and every local schema/config reference are declared inputs. The caller
declares the shared tsconfig and runtime dependencies; the named target is the compiled library:

```starlark
load("//devinfra/js:openapi_zod_ts_library.bzl", "openapi_zod_ts_library")

openapi_zod_ts_library(
    name = "schema_zod",
    schema = ":openapi_schema",
    config = "openapi-ts.config.mjs",
    tsconfig = ":tsconfig",
    deps = ["//:node_modules/zod"],
    schema_files = [":referenced_schema"],
    config_files = [":local_config_helper"],
)
```

The raw generated source is also available by file label, `:schema_zod.ts` by default. JavaScript
consumers import the compiled `schema_zod.js`; TypeScript consumers resolve `schema_zod.d.ts`.
Set `out` to choose another source path. An `.mts` output selects `.mjs` and `.d.mts` when an
integration specifically requires those extensions.

The config is an ES module with one default-exported configuration object. List local modules it
imports in `config_files` and imported runtime packages in `config_deps`. The converter uses Hey
API's config-file API with the declared schema path; plugin settings stay in the config.

Both OpenAPI launchers use the shared remote-reference guard. They fail closed if a generator
attempts HTTP resolution during the build. The production packages expose the conversion interfaces;
generator-specific fixtures and no-fetch proof live in the separate schema spike PR #12342.

Remote references are disabled during generation. Schema exporters remain responsible for their
source contract and serialization choices; this rule does not normalize schema dialects or merge
OpenAPI and JSON Schema inputs. Reference interpretation otherwise follows the pinned OpenAPI
parser. In particular, it may treat a literal `$ref` key nested inside example or default data as a
schema reference; such payloads are not claimed as supported by this conversion path.

## Standalone JSON Schema declarations

The `json_schema_js_library` macro converts one declared JSON Schema file without invoking its
producer and packages the declaration in a `js_library` under the requested target name. It has the
same file-input shape as `openapi_js_library`:

```starlark
load("//devinfra/js:json_schema_js_library.bzl", "json_schema_js_library")

json_schema_js_library(
    name = "types",
    schema = "//my/api:types_schema",
    schema_files = ["//my/api:shared_definitions"],
    out = "api/types.d.ts",
)
```

List every file reached through a relative local `$ref` in `schema_files`; the action receives each
as a declared input and resolves the reference from the root schema's path. Remote HTTP resolution
is disabled by the converter.

When a runtime consumer needs the source JSON as well as generated declarations, keep the schema
artifact as its own Bazel input. A wrapper can package the JSON source and depend on the declaration
library:

```starlark
load("@bazel_skylib//rules:copy_file.bzl", "copy_file")
load("//devinfra/js:json_schema_js_library.bzl", "json_schema_js_library")
load("@aspect_rules_js//js:defs.bzl", "js_library")

json_schema_js_library(
    name = "tool_arguments_types",
    schema = ":types_schema",
    out = "api/types.d.ts",
)

copy_file(
    name = "_schema_json",
    src = ":types_schema",
    out = "api/types.schema.json",
)

js_library(
    name = "tool_arguments",
    srcs = [":_schema_json"],
    deps = [":tool_arguments_types"],
)
```

JSON Schema carries no HTTP operation types; owners remain responsible for exporting their actual
runtime or API contract.
