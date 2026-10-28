"""Schema-file-based TypeScript generators used only by this spike."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("@npm_ducktape//:@hey-api/openapi-ts/package_json.bzl", openapi_ts_bin = "bin")
load("@npm_ducktape//:json-schema-to-typescript/package_json.bzl", json_schema_ts_bin = "bin")
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

def openapi_zod_from_file(name, schema, config, out):
    """Emit a Zod module from an existing OpenAPI JSON artifact."""
    schema_path = _package_path(schema)
    generated = "_" + name + "_generated/zod.gen.ts"
    openapi_ts_bin.openapi_ts_binary(name = "_" + name + "_bin")
    js_run_binary(
        name = "_" + name + "_generate",
        srcs = [schema, config],
        outs = [generated],
        args = ["--input", schema_path, "--output", "_" + name + "_generated"],
        chdir = native.package_name(),
        tool = ":_" + name + "_bin",
    )
    native.genrule(
        name = name,
        srcs = [":" + "_" + name + "_generate"],
        outs = [out],
        cmd = "cp $< $@",
    )

def json_schema_types_from_file(name, schema, out):
    """Emit declarations from an existing JSON Schema artifact."""
    schema_path = _package_path(schema)
    json_schema_ts_bin.json2ts_binary(name = "_" + name + "_bin")
    js_run_binary(
        name = name,
        srcs = [schema],
        outs = [out],
        args = [schema_path, "-o", out, "--bannerComment", ""],
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
