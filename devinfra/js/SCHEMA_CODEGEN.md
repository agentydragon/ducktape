# TypeScript schema code generation

The file-input rules in this package convert declared OpenAPI or JSON Schema artifacts into
TypeScript source. Owner-specific targets produce the schema files; normal language rules compile
the generated source. These rules do not invoke a backend exporter and do not merge schema formats
into a shared intermediate representation.

## Bazel interfaces

Load the converter that matches the source format:

```starlark
load("//devinfra/js:openapi_typescript.bzl", "openapi_typescript")
load("//devinfra/js:openapi_zod.bzl", "openapi_zod")
load("//devinfra/js:json_schema_typescript.bzl", "json_schema_typescript")
```

Each `schema` and `config` argument names a single-file Bazel target. It can be a source file, a
`filegroup`, a `genrule` output, or another file-producing target in this or another package. The
macros pass paths through Bazel `$(location ...)` expansion; callers should pass labels directly
and should not construct action paths from package names or output filenames.

- `openapi_typescript(name, schema, out, schema_files = [], visibility = None)` emits an OpenAPI
  declaration file with `paths`, `components`, operations, and root model exports. It uses the
  pinned `openapi-typescript` options `rootTypes` and `rootTypesNoSchemaPrefix`; it deliberately does
  not use `rootTypesKeepCasing`.
- `openapi_zod(name, schema, config, out, schema_files = [], config_files = [], config_deps = [], visibility = None)`
  emits the configured Hey API Zod TypeScript source. Its config file must be an ES module whose
  default export is one configuration object (not an array or function). Put local modules imported
  by that config in `config_files`, and runtime package targets used by it in `config_deps`; all three lists are declared
  as conversion action inputs.
- `json_schema_typescript(name, schema, out, schema_files = [], visibility = None)` emits model
  declarations from a standalone JSON Schema document.

List every file reached through a relative local `$ref` in `schema_files`; these are declared
inputs of the conversion action. A Zod config's relative imports are declared in `config_files`.
The config, helper files, and schema can each come from generated or cross-package targets. Tool
packages are declared by the converter binaries in this package; `config_deps` is for additional
packages imported by a caller's config. Use exact Bazel npm labels such as
`//:node_modules/example-package`.

The converters emit source files only. Add a normal `ts_library` for generated Zod so TypeScript
produces the JavaScript module and declarations. Add a `js_library` for declaration-only output when
consumers need the declarations as a dependency. Keep the frontend's existing `ts_library` or
`js_library` structure responsible for module format, declarations, and type checking.

## References and configuration

Relative local references are resolved by the pinned generator from the input file's path. Declaring
those files in `schema_files` makes them available to the sandboxed action; it does not rewrite the
reference graph. OpenAPI and JSON Schema inputs are passed as files, so an absolute `$id` on a local
root document remains metadata and does not cause that document to be fetched.

Build-time remote resolution is disabled. The OpenAPI launchers temporarily replace the process
`fetch` with a response that causes the upstream resolver to fail, then restore it. The JSON Schema
launcher disables the HTTP resolver in `json-schema-to-typescript`. The production launcher test
uses a loopback HTTP server to prove remote-reference conversions fail without making a request,
while local references and absolute `$id` file inputs still convert.

Reference and schema interpretation otherwise remains the pinned generators' behavior. There is
no recursive rewrite or universal schema normalizer. In particular, the upstream OpenAPI parsers
may interpret `$ref` keys nested in example/default data as references; such literal payloads are not
claimed as supported by this conversion path. Add a focused reproducer and resolve that upstream
behavior before relying on such data.

An OpenAPI Zod config's top-level default export is restricted to an object because the launcher
passes the config module to Hey API through its supported config-file API. Keep the plugin options in
that object, including date policies; command-line plugin flags would override the owner's settings.
The config file itself is a declared input, and imported local modules or runtime packages must be
declared through `config_files` and `config_deps`.

## Boundaries

OpenAPI declaration generation preserves HTTP operation and parameter types; standalone JSON Schema
does not carry those concepts. SSH tool arguments and results use direct standalone JSON Schema and
keep renderer strictness at the runtime Zod adapter. FastAPI, MCP, and model owners remain responsible
for exporting their actual contracts and selecting serialization mode or dialect.

Native protobuf/RPC generation, Swagger 2 documentation, Kubernetes structural schemas, Grocy's
FastMCP OpenAPI import, provider-specific schemas, and Google Discovery remain on their existing
owner-specific paths. These converters are not a universal schema toolchain.
