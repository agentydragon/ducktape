"""Convert a JSON Schema document file into TypeScript declarations."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")

def json_schema_typescript(name, schema, out, schema_files = [], visibility = None):
    """Generate declarations from a declared JSON Schema file.

    `schema_files` declares each file reached by a relative local `$ref`.
    The converter accepts single-file Bazel targets and passes Bazel-expanded
    paths directly, preserving local relative-reference resolution.
    """
    schema_label = str(schema)

    js_run_binary(
        name = name,
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
