"""Convert an OpenAPI document file into TypeScript declarations."""

load(":schema_declarations.bzl", "schema_declarations")

def openapi_js_library(name, schema, out = None, schema_files = [], visibility = None):
    """Generate API declarations from a declared OpenAPI JSON file.

    `schema` and `schema_files` are single-file Bazel targets. List every file
    referenced from `schema` by a relative local `$ref` in `schema_files`.
    `$(location)` expansion keeps source, generated, and cross-package labels
    opaque to this macro while making all of them action inputs. The named
    target publishes the declarations through `JsInfo`; the generated file is
    also available by its `out` label, which defaults to `<name>.d.ts`.
    """
    schema_declarations(
        name = name,
        schema = schema,
        schema_files = schema_files,
        out = out,
        tool = "//devinfra/js:openapi_typescript_codegen",
        publish_schema = False,
        visibility = visibility,
    )
