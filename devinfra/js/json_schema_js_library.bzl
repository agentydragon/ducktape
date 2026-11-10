"""Convert a JSON Schema document file into TypeScript declarations."""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")

def json_schema_js_library(name, schema, out = None, schema_files = [], visibility = None):
    """Generate declarations from a declared JSON Schema file.

    `schema_files` declares each file reached by a relative local `$ref`.
    The converter accepts single-file Bazel targets and passes Bazel-expanded
    paths directly, preserving local relative-reference resolution. The named
    target publishes the schema, its declared references, and generated
    declarations through `JsInfo`; the generated file is also available by its
    `out` label, which defaults to `<name>.d.ts`.
    """
    if out == None:
        out = name + ".d.ts"

    schema_label = str(schema)

    generated_name = "_{}_generate".format(name)
    js_run_binary(
        name = generated_name,
        srcs = [schema] + schema_files,
        outs = [out],
        args = [
            "$(location {})".format(schema_label),
            "$(location :{})".format(out),
        ],
        chdir = "../../../",
        tool = "//devinfra/js:json_schema_typescript_codegen",
        visibility = visibility,
    )

    js_library(
        name = name,
        srcs = [schema] + schema_files + [":" + generated_name],
        visibility = visibility,
    )
