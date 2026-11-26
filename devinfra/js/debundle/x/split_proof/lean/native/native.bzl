"""Per-module native Lean artifacts for the pinned Linux Lean distribution.

Dependencies must enumerate the Lean modules available for import. Each module
has its own Lean and C compilation actions, so unrelated modules are cacheable.
This rule does not implement Lean plugins or generated proof verification.
"""

NativeLeanInfo = provider(fields = {
    "modules": "dict from full Lean module name to its olean artifact",
    "objects": "transitive depset of native object files",
})

def _cc_flags(tc):
    # The pinned Linux Lean 4.35 distribution contains clang and its sysroot
    # next to bin/lean. These are its configured native compiler flags.
    root = tc.lean.dirname.removesuffix("/bin")
    return [
        "-I",
        root + "/include",
        "-fstack-clash-protection",
        "-ffp-contract=off",
        "-fdata-sections",
        "-ffunction-sections",
        "-fPIC",
        "-fvisibility=hidden",
        "--sysroot",
        root,
        "-nostdinc",
        "-isystem",
        root + "/include/clang",
    ]

def _link_flags(tc):
    root = tc.lean.dirname.removesuffix("/bin")
    return [
        "--sysroot",
        root,
        "-L",
        root + "/lib",
        "-L",
        root + "/lib/glibc",
        "-lc",
        "-lc_nonshared",
        "-Wl,--as-needed",
        "-l:ld.so",
        "-Wl,--no-as-needed",
        "-lpthread_nonshared",
        "-Wl,--as-needed",
        "-Wl,-Bstatic",
        "-lgmp",
        "-lunwind",
        "-luv",
        "-lssl",
        "-lcrypto",
        "-Wl,-Bdynamic",
        "-Wl,--no-as-needed",
        "-fuse-ld=lld",
        "-L",
        root + "/lib/lean",
        "-Wl,--start-group",
        "-lleancpp",
        "-lLean",
        "-Wl,--end-group",
        "-lStd",
        "-Wl,--start-group",
        "-lInit",
        "-lleanrt",
        "-Wl,--end-group",
        "-Wl,-Bstatic",
        "-lc++",
        "-lc++abi",
        "-Wl,-Bdynamic",
        "-lLake",
        "-lLeanExport",
        "-Wl,--as-needed",
        "-lgmp",
        "-luv",
        "-lssl",
        "-lcrypto",
        "-lpthread",
        "-ldl",
        "-lrt",
        "-Wl,--no-as-needed",
        "-Wl,--gc-sections",
        "-lm",
        "-ldl",
        "-pthread",
    ]

def _module_impl(ctx):
    tc = ctx.toolchains["@rules_lean//lean:toolchain_type"].leantc
    if tc.native_cc == None:
        fail("selected Lean toolchain does not provide a native compiler")
    module = ctx.attr.module
    rel = module.replace(".", "/")
    if not module or not ctx.file.src.short_path.endswith(rel + ".lean"):
        fail("src must end with the full module path for %s" % module)
    modules = {}
    for dep in ctx.attr.deps:
        for name, artifact in dep[NativeLeanInfo].modules.items():
            if name in modules and modules[name] != artifact:
                fail("multiple artifacts for Lean module %s" % name)
            modules[name] = artifact
    if module in modules:
        fail("module %s also appears in deps" % module)

    root = ctx.label.name + "_lean"
    src = ctx.actions.declare_file(root + "/" + rel + ".lean")
    ctx.actions.symlink(output = src, target_file = ctx.file.src)
    staged = []
    for name, artifact in modules.items():
        dest = ctx.actions.declare_file(root + "/" + name.replace(".", "/") + ".olean")
        ctx.actions.symlink(output = dest, target_file = artifact)
        staged.append(dest)
    olean = ctx.actions.declare_file(root + "/" + rel + ".olean")
    c_file = ctx.actions.declare_file(ctx.label.name + ".c")
    obj = ctx.actions.declare_file(ctx.label.name + ".o")
    root_path = src.path[:-len(rel + ".lean")].removesuffix("/")
    ctx.actions.run(
        executable = ctx.executable._compile,
        arguments = [tc.lean.path, root_path, src.path, olean.path, c_file.path],
        inputs = depset([src, tc.lean] + staged, transitive = [tc.runtime]),
        outputs = [olean, c_file],
        mnemonic = "LeanNativeModule",
        progress_message = "Compiling Lean module %s" % module,
    )
    ctx.actions.run(
        executable = tc.native_cc,
        arguments = _cc_flags(tc) + ["-c", c_file.path, "-o", obj.path],
        inputs = depset([c_file], transitive = [tc.runtime]),
        outputs = [obj],
        mnemonic = "LeanNativeObject",
        progress_message = "Compiling Lean native object %s" % module,
    )
    modules[module] = olean
    return [
        DefaultInfo(files = depset([olean, c_file, obj])),
        NativeLeanInfo(
            modules = modules,
            objects = depset([obj], transitive = [d[NativeLeanInfo].objects for d in ctx.attr.deps]),
        ),
    ]

native_lean_module = rule(
    implementation = _module_impl,
    attrs = {
        "src": attr.label(mandatory = True, allow_single_file = [".lean"]),
        "module": attr.string(mandatory = True),
        "deps": attr.label_list(providers = [NativeLeanInfo]),
        "_compile": attr.label(default = Label("//devinfra/js/debundle/x/split_proof/lean/native:compile_driver"), executable = True, cfg = "exec"),
    },
    toolchains = ["@rules_lean//lean:toolchain_type"],
)

def _binary_impl(ctx):
    tc = ctx.toolchains["@rules_lean//lean:toolchain_type"].leantc
    if tc.native_cc == None:
        fail("selected Lean toolchain does not provide a native compiler")
    objects = ctx.attr.root[NativeLeanInfo].objects
    exe = ctx.actions.declare_file(ctx.label.name)
    args = ctx.actions.args()
    args.add("-o", exe)
    args.add_all(objects)
    ctx.actions.run(
        executable = tc.native_cc,
        arguments = _cc_flags(tc) + [args] + _link_flags(tc),
        inputs = depset(transitive = [objects, tc.runtime]),
        outputs = [exe],
        mnemonic = "LeanNativeLink",
        progress_message = "Linking Lean executable %s" % ctx.label,
    )
    return [DefaultInfo(executable = exe, runfiles = ctx.runfiles())]

native_lean_binary = rule(
    implementation = _binary_impl,
    executable = True,
    attrs = {
        "root": attr.label(mandatory = True, providers = [NativeLeanInfo]),
    },
    toolchains = ["@rules_lean//lean:toolchain_type"],
)
