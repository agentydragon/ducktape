# OpenAPI TypeScript declarations

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

`openapi_zod` converts a declared OpenAPI file into the Zod TypeScript source selected by a local
Hey API config. The config and every local schema/config reference are declared inputs. Compile the
generated source with an ordinary `ts_library`; for an `.mts` source it emits both `.mjs` and
`.d.mts` for JavaScript and TypeScript consumers:

```starlark
load("//devinfra/js:openapi_zod.bzl", "openapi_zod")
load("//devinfra/js:ts_library.bzl", "ts_library")

openapi_zod(
    name = "schema_zod_codegen",
    schema = ":openapi_schema",
    config = "openapi-ts.config.mjs",
    schema_files = [":referenced_schema"],
    config_files = [":local_config_helper"],
    out = "api/schema.zod.mts",
)

ts_library(
    name = "schema_zod",
    srcs = [":schema_zod_codegen"],
    tsconfig = ":tsconfig",
    deps = ["//:node_modules/zod"],
)
```

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
