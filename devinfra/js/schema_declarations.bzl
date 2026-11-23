"""Shared action for schema-to-TypeScript declaration generators."""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")

def schema_declarations(name, schema, schema_files, out, tool, publish_schema, visibility):
    """Run the selected converter on declared files and expose its .d.ts through JsInfo."""
    if out == None:
        out = name + ".d.ts"

    inputs = [schema] + schema_files
    generated_name = "_{}_generate".format(name)
    js_run_binary(
        name = generated_name,
        srcs = inputs,
        outs = [out],
        args = [
            "$(location {})".format(schema),
            "$(location :{})".format(out),
        ],
        chdir = "../../../",
        tool = tool,
        visibility = visibility,
    )

    js_library(
        name = name,
        srcs = (inputs if publish_schema else []) + [":" + generated_name],
        visibility = visibility,
    )
