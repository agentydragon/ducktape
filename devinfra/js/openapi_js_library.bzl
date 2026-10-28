"""Convert an OpenAPI document file into TypeScript declarations."""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")

def openapi_js_library(name, schema, out = None, schema_files = [], visibility = None):
    """Generate API declarations from a declared OpenAPI JSON file.

    `schema` and `schema_files` are single-file Bazel targets. List every file
    referenced from `schema` by a relative local `$ref` in `schema_files`.
    `$(location)` expansion keeps source, generated, and cross-package labels
    opaque to this macro while making all of them action inputs. The named
    target publishes the declarations through `JsInfo`; the generated file is
    also available by its `out` label, which defaults to `<name>.d.ts`.
    """
    if out == None:
        out = name + ".d.ts"

    schema_label = str(schema)
    output_label = ":" + out

    generated_name = "_{}_generate".format(name)
    js_run_binary(
        name = generated_name,
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

    js_library(
        name = name,
        srcs = [":" + generated_name],
        visibility = visibility,
    )
