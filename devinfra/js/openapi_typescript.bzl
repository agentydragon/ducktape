"""Convert an OpenAPI document file into TypeScript declarations."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")

def openapi_typescript(name, schema, out, schema_files = [], visibility = None):
    """Generate API declarations from a declared OpenAPI JSON file.

    `schema` and `schema_files` are single-file Bazel targets. List every file
    referenced from `schema` by a relative local `$ref` in `schema_files`.
    `$(location)` expansion keeps source, generated, and cross-package labels
    opaque to this macro while making all of them action inputs.
    """
    schema_label = str(schema)
    output_label = ":" + out

    js_run_binary(
        name = name,
        srcs = [schema] + schema_files,
        outs = [out],
        args = [
            "$(location {})".format(schema_label),
            "$(location {})".format(output_label),
        ],
        chdir = "../../../",
        tool = "//devinfra/js:openapi_typescript_codegen",
        visibility = visibility,
    )
