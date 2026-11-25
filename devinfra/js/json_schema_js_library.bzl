"""Convert a JSON Schema document file into TypeScript declarations."""

load(":schema_declarations.bzl", "schema_declarations")

def json_schema_js_library(name, schema, out = None, schema_files = [], visibility = None):
    """Generate declarations from a declared JSON Schema file.

    `schema_files` declares each file reached by a relative local `$ref`.
    The converter accepts single-file Bazel targets and passes Bazel-expanded
    paths directly, preserving local relative-reference resolution. The named
    target publishes the schema, its declared references, and generated
    declarations through `JsInfo`; the generated file is also available by its
    `out` label, which defaults to `<name>.d.ts`.
    """
    schema_declarations(
        name = name,
        schema = schema,
        schema_files = schema_files,
        out = out,
        tool = "//devinfra/js:json_schema_typescript_codegen",
        publish_schema = True,
        visibility = visibility,
    )
