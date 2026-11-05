"""Configured compiled inputs shared by the analysis writer and reader.

This is deliberately scoped to the pinned rules_rust closure, not an action key
or a digest of the final executable (which package managers may rewrite).
"""

load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_rust//rust:defs.bzl", "rust_common")
load("@rules_rust//rust/private:pic_utils.bzl", "should_use_pic")
load("@rules_rust//rust/private:utils.bzl", "get_preferred_artifact")

def _analysis_identity_impl(ctx):
    engine = ctx.attr.engine
    crate = engine[rust_common.crate_info]
    dep = engine[rust_common.dep_info]
    if crate.type not in ["lib", "rlib"]:
        fail("analysis identity requires a compiled Rust library")
    rust = ctx.toolchains["@rules_rust//rust:toolchain_type"]
    if rust._experimental_use_cc_common_link or rust.linker_preference == "rust":
        fail("analysis identity supports the pinned rustc-driven C++ linker policy only")
    cc = find_cc_toolchain(ctx)

    # Match rules_rust 0.74 utils.bzl's unsupported features. The engine has
    # native dependencies, so rustc does not disable rules_rust_link_cc for
    # its consuming binary.
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features + [
            "thin_lto",
            "module_maps",
            "use_header_modules",
            "fdo_instrument",
            "fdo_optimize",
            "rules_rust_unsupported_feature",
        ],
    )
    variables = cc_common.create_link_variables(
        feature_configuration = features,
        cc_toolchain = cc,
        is_linking_dynamic_library = False,
        user_link_flags = ctx.fragments.cpp.linkopts,
    )
    config = {
        "linker": cc_common.get_tool_for_action(feature_configuration = features, action_name = "c++-link-executable"),
        "link_args": cc_common.get_memory_inefficient_command_line(feature_configuration = features, action_name = "c++-link-executable", variables = variables),
        "link_env": cc_common.get_environment_variables(feature_configuration = features, action_name = "c++-link-executable", variables = variables),
        "rust_env": rust.env,
        "rust_linker_preference": rust.linker_preference,
        "rust_linker_type": rust.linker_type,
        "stdlib_link_flags": [
            [str(linker.owner), linker.user_link_flags]
            for linker in rust.stdlib_linkflags.linking_context.linker_inputs.to_list()
        ],
        "native": [],
    }
    files = [crate.output]
    if rust.target_json:
        files.append(rust.target_json)
    use_pic = should_use_pic(
        cc_toolchain = cc,
        feature_configuration = features,
        crate_type = "bin",
        compilation_mode = ctx.var["COMPILATION_MODE"],
        toolchain = rust,
    )
    transitive = [
        crate.compile_data,
        crate.data,
        dep.transitive_crate_outputs,
        dep.transitive_data,
        dep.transitive_proc_macro_data,
        dep.link_search_path_files,
        rust.all_files,
        cc.all_files,
    ]
    static_runtime = cc.static_runtime_lib(feature_configuration = features)
    dynamic_runtime = cc.dynamic_runtime_lib(feature_configuration = features)
    transitive.extend([static_runtime, dynamic_runtime])
    for library in dep.transitive_crates.to_list() + [crate]:
        transitive.extend([library.compile_data, library.data])
        files.extend(library.rustc_env_files)
    for build in dep.transitive_build_infos.to_list():
        transitive.append(build.compile_data)
        for field in ["out_dir", "dep_env", "flags", "link_search_paths", "linker_flags", "rustc_env"]:
            file = getattr(build, field)
            if file:
                files.append(file)
    for linker in dep.transitive_noncrates.to_list():
        if linker.linkstamps:
            fail("analysis identity does not support native linkstamps: {}".format(linker.owner))
        native = {"owner": str(linker.owner), "flags": linker.user_link_flags, "libraries": []}
        config["native"].append(native)
        files.extend(linker.additional_inputs)
        for library in linker.libraries:
            # Use the pinned rules_rust selection policy, just as collect_inputs
            # does. Hashing every variant would build unused shared libraries.
            file = get_preferred_artifact(library, use_pic)
            if not file:
                fail("analysis identity does not support an object-only native library: {}".format(linker.owner))
            files.append(file)
            native["libraries"].append({"file": file.path, "alwayslink": library.alwayslink})
            if file == library.interface_library and library.dynamic_library:
                files.append(library.dynamic_library)
    inputs = depset(files, transitive = transitive)
    cached_inputs = depset(transitive = [
        rust.all_files,
        cc.all_files,
        static_runtime,
        dynamic_runtime,
    ])
    all_input_files = inputs.to_list()
    all_paths = sorted([file.path for file in all_input_files])
    cached_paths = sorted([file.path for file in cached_inputs.to_list()])
    cached_path_set = {path: True for path in cached_paths}
    all_path_set = {path: True for path in all_paths}
    for path in cached_paths:
        if path not in all_path_set:
            fail("analysis identity cached toolchain input is missing from the full closure: {}".format(path))
    non_cached_inputs = [file for file in all_input_files if file.path not in cached_path_set]

    cached_manifest = ctx.actions.declare_file(ctx.label.name + ".toolchain.manifest.json")
    cached_records = ctx.actions.declare_file(ctx.label.name + ".toolchain.records.json")
    ctx.actions.write(cached_manifest, json.encode({"files": cached_paths}))
    ctx.actions.run(
        executable = ctx.executable._hasher,
        arguments = ["cache", cached_manifest.path, cached_records.path],
        inputs = depset([cached_manifest], transitive = [cached_inputs]),
        outputs = [cached_records],
        mnemonic = "DebundleAnalysisIdentityToolchain",
    )

    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    output = ctx.actions.declare_file(ctx.label.name + ".rs")
    ctx.actions.write(manifest, json.encode({
        "config": config,
        "files": all_paths,
        "cached_files": cached_paths,
    }))
    ctx.actions.run(
        executable = ctx.executable._hasher,
        arguments = ["hash", manifest.path, cached_records.path, output.path],
        inputs = depset([manifest, cached_records] + non_cached_inputs),
        outputs = [output],
        mnemonic = "DebundleAnalysisIdentity",
    )
    return [DefaultInfo(files = depset([output]))]

analysis_identity = rule(
    implementation = _analysis_identity_impl,
    attrs = {
        "engine": attr.label(mandatory = True, providers = [rust_common.crate_info, rust_common.dep_info]),
        "_hasher": attr.label(default = ":analysis_identity_hasher_bin", executable = True, cfg = "exec"),
    },
    fragments = ["cpp"],
    toolchains = ["@rules_rust//rust:toolchain_type", "@bazel_tools//tools/cpp:toolchain_type"],
)
