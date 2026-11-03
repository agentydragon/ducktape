"""Experiment: invoke Istio's protobuf-to-CRD compiler on a proto_library."""

load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _proto_crd_impl(ctx):
    info = ctx.attr.proto[ProtoInfo]
    if len(info.direct_sources) != 1:
        fail("expected one source", "proto")
    output = ctx.actions.declare_file("kubernetes/customresourcedefinitions.gen.yaml")
    import_roots = ["-I" + root for root in info.transitive_proto_path.to_list() if root != "."]
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "-I.",
            "--plugin=protoc-gen-crd=" + ctx.executable._plugin.path,
            "--crd_out=" + ctx.bin_dir.path + "/" + ctx.label.package,
        ] + import_roots + [info.direct_sources[0].path],
        inputs = info.transitive_sources,
        outputs = [output],
        tools = [ctx.executable._plugin],
        mnemonic = "PrototypeProtoCRD",
        use_default_shell_env = True,
    )
    return DefaultInfo(files = depset([output]))

proto_crd = rule(
    implementation = _proto_crd_impl,
    attrs = {
        "proto": attr.label(mandatory = True, providers = [ProtoInfo]),
        "_plugin": attr.label(default = Label("@io_istio_tools//cmd/protoc-gen-crd"), executable = True, cfg = "exec"),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
)

def _unsupported_listvalue_impl(ctx):
    info = ctx.attr.proto[ProtoInfo]
    if len(info.direct_sources) != 1:
        fail("expected one source", "proto")
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    import_roots = ["-I" + root for root in info.transitive_proto_path.to_list() if root != "."]
    ctx.actions.run_shell(
        inputs = info.transitive_sources,
        tools = [ctx.executable._plugin, ctx.executable._protoc],
        outputs = [output],
        arguments = [output.path, ctx.executable._protoc.path, ctx.executable._plugin.path, info.direct_sources[0].path] + import_roots,
        command = """
set -eu
out="$1"; protoc="$2"; plugin="$3"; source="$4"; shift 4
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if "$protoc" -I. "--plugin=protoc-gen-crd=$plugin" "--crd_out=$work" "$@" "$source" >"$work/log" 2>&1; then
  echo "ListValue CRD unexpectedly succeeded; re-evaluate the prototype" >&2
  exit 1
fi
if ! grep -F 'additionalProperties.type' "$work/log" >/dev/null; then
  cat "$work/log" >&2
  exit 1
fi
printf '%s\\n' 'Istio protoc-gen-crd rejected the Action list value schema as non-structural (additionalProperties.type).' > "$out"
""",
        mnemonic = "ProbeUnsupportedProtoCRD",
        use_default_shell_env = True,
    )
    return DefaultInfo(files = depset([output]))

unsupported_listvalue_probe = rule(
    implementation = _unsupported_listvalue_impl,
    attrs = {
        "proto": attr.label(mandatory = True, providers = [ProtoInfo]),
        "_plugin": attr.label(default = Label("@io_istio_tools//cmd/protoc-gen-crd"), executable = True, cfg = "exec"),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
)
