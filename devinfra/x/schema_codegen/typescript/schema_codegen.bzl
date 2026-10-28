"""Schema-file-based TypeScript generators used only by this spike."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("@npm_ducktape//:openapi-typescript/package_json.bzl", openapi_typescript_bin = "bin")

def _package_path(label):
    """Return the filename for a same-package direct file label only."""
    if not label.startswith(":") or "/" in label[1:] or not label[1:]:
        fail("schema_codegen converters accept same-package direct file labels only; got %r" % label)
    return label[1:]

def openapi_types_from_file(name, schema, out, keep_casing = False):
    """Emit OpenAPI declarations from an existing JSON artifact."""
    schema_path = _package_path(schema)
    openapi_typescript_bin.openapi_typescript_binary(name = "_" + name + "_bin")
    args = [
        schema_path,
        "-o",
        out,
        "--root-types",
        "--root-types-no-schema-prefix",
    ]
    if keep_casing:
        args.append("--root-types-keep-casing")
    js_run_binary(
        name = name,
        srcs = [schema],
        outs = [out],
        args = args,
        chdir = native.package_name(),
        tool = ":_" + name + "_bin",
    )

def literal_types_from_file(name, schema, out):
    """Emit a const tuple and its union type from a string-enum JSON Schema."""
    schema_path = _package_path(schema)
    js_run_binary(
        name = name,
        srcs = [schema],
        outs = [out],
        args = [schema_path, out],
        chdir = native.package_name(),
        tool = ":literal_codegen_bin",
    )
