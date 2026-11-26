"""Build the fixed verifier runfile map used by the native splitter."""

load("@rules_lean//lean:defs.bzl", "LeanInfo")
load("@rules_lean//lean:toolchain.bzl", "LEAN_TOOLCHAIN_TYPE")

def _runfile_key(ctx, file):
    path = file.short_path
    if path.startswith("../"):
        return path.removeprefix("../")
    return ctx.workspace_name + "/" + path

def _path_pair(exec_path, runfile_key):
    return "({}, {})".format(json.encode(exec_path), json.encode(runfile_key))

def _generated_proof_toolchain_impl(ctx):
    tc = ctx.toolchains[LEAN_TOOLCHAIN_TYPE]
    oleans = {}
    for dep in ctx.attr.deps:
        for module, olean in dep[LeanInfo].module_oleans.to_list():
            if module in oleans and oleans[module] != olean:
                fail("multiple .oleans for generated proof import " + module)
            oleans[module] = olean

    executable = ctx.label.package + "/" + ctx.attr.binary_name
    source = ctx.actions.declare_file("EsmSplit/LowerSplit/BazelProofConfig.lean")
    ctx.actions.write(source, """namespace EsmSplit.LowerSplit.BazelProofConfig

-- Each pair is the artifact path in the execroot and in the runfiles tree.
def executablePaths : String × String := {executable}

def leanPaths : String × String := {lean}

def importPaths : Array (String × (String × String)) := #[
{imports}
]

end EsmSplit.LowerSplit.BazelProofConfig
""".format(
        executable = _path_pair(ctx.bin_dir.path + "/" + executable, ctx.workspace_name + "/" + executable),
        lean = _path_pair(tc.lean.path, _runfile_key(ctx, tc.lean)),
        imports = ",\n".join([
            "  ({}, {})".format(json.encode(module), _path_pair(olean.path, _runfile_key(ctx, olean)))
            for module, olean in sorted(oleans.items())
        ]),
    ))
    proof_files = depset([tc.lean] + oleans.values(), transitive = [tc.lean_files])
    return [
        DefaultInfo(files = depset([source])),
        OutputGroupInfo(proof_files = proof_files),
    ]

generated_proof_toolchain = rule(
    implementation = _generated_proof_toolchain_impl,
    attrs = {
        "binary_name": attr.string(mandatory = True),
        "deps": attr.label_list(providers = [LeanInfo], mandatory = True),
    },
    toolchains = [LEAN_TOOLCHAIN_TYPE],
)
