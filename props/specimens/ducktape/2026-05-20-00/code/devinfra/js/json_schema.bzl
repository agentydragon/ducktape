"""Macro: js_json_schema — generate TypeScript types from a JSON-Schema-emitting binary."""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@npm_ducktape//:json-schema-to-typescript/package_json.bzl", json_schema_ts_bin = "bin")

def js_json_schema(name, generator, out = "types.d.ts", visibility = None):
    """Generate a js_library with TypeScript type definitions from a JSON Schema.

    Runs *generator* (a binary that prints a JSON Schema to stdout),
    then pipes the result through json-schema-to-typescript to produce
    a ``.d.ts`` file, and wraps that in a ``js_library`` target.

    Args:
        name:       Name of the output ``js_library`` target.
        generator:  Label of the executable that writes JSON Schema to stdout.
        out:        Package-relative path for the generated ``.d.ts`` file.
                    Defaults to ``types.d.ts``.
        visibility: Visibility of the output ``js_library``.

    Example:
        js_json_schema(
            name = "types",
            generator = "//my/backend:export_schema_bin",
        )
    """
    json_out = "_" + name + "_schema.json"

    json_schema_ts_bin.json2ts_binary(
        name = "_" + name + "_json_schema_ts_bin",
    )

    native.genrule(
        name = "_" + name + "_schema_json",
        outs = [json_out],
        cmd = "$(location {}) > $@".format(generator),
        tools = [generator],
    )

    js_run_binary(
        name = "_" + name + "_generate",
        srcs = [":_" + name + "_schema_json"],
        outs = [out],
        args = [
            json_out,
            "-o",
            out,
            "--bannerComment",
            "",
        ],
        chdir = native.package_name(),
        tool = ":_" + name + "_json_schema_ts_bin",
    )

    js_library(
        name = name,
        srcs = [":_" + name + "_generate"],
        tags = ["no-lint"],
        visibility = visibility,
    )
