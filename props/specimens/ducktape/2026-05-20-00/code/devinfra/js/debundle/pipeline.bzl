"""Bazel-driven debundle pipeline rule.

Runs the ducktape debundler with `--out-root` pointing at a tree-artifact
output directory. All pipeline outputs (manifests, analysis, emitted JS) land
under the rule's declared output directory in `bazel-bin/`. Each corpus may
layer a `write_source_files` regen target on top to commit a subset of outputs
into the source tree.
"""

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//lib:shell.bzl", "shell")

def _debundle_pipeline_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.label.name + ".out")
    bin_dir = ctx.bin_dir.path
    plan = _debundle_pipeline_plan(ctx, out_dir.short_path)

    ctx.actions.run_shell(
        inputs = plan.inputs,
        tools = [ctx.executable.debundler],
        outputs = [out_dir],
        command = "cd \"${{BAZEL_BINDIR}}\" && exec {command}".format(
            command = plan.command,
        ),
        env = {"BAZEL_BINDIR": bin_dir},
        # The debundler asserts that each vendor package's resolved subpath
        # canonicalizes to a location within the package root. Inside
        # Bazel's linux-sandbox, package-dir entries are real directories
        # but their leaf files are symlinks to the host execroot's bazel-bin
        # — so `realpath(file)` lands outside `realpath(dir)` and the check
        # spuriously fails. Disable sandboxing for this action; inputs are
        # declared via Bazel attrs, so reproducibility is preserved.
        execution_requirements = {"no-sandbox": "1"},
        progress_message = "Running debundle pipeline for %{label}",
        mnemonic = "DebundlePipeline",
    )

    return [DefaultInfo(files = depset([out_dir]))]

def _debundle_pipeline_profile_impl(ctx):
    profile_dir = ctx.actions.declare_directory(ctx.label.name + ".profile")
    bin_dir = ctx.bin_dir.path
    plan = _debundle_pipeline_plan(
        ctx,
        paths.join(profile_dir.short_path, "debundle.out"),
    )

    ctx.actions.run_shell(
        inputs = plan.inputs,
        tools = [ctx.executable.debundler],
        outputs = [profile_dir],
        command = _profile_command(
            ctx.attr.profile,
            shell.quote(profile_dir.short_path),
            plan.command,
        ),
        env = {"BAZEL_BINDIR": bin_dir},
        execution_requirements = {
            "local": "1",
            "no-cache": "1",
            "no-remote": "1",
            "no-sandbox": "1",
        },
        progress_message = "Profiling debundle pipeline for %{{label}} with {}".format(ctx.attr.profile),
        mnemonic = "DebundlePipelineProfile",
        use_default_shell_env = True,
    )

    return [DefaultInfo(files = depset([profile_dir]))]

def _debundle_pipeline_plan(ctx, out_root):
    bin_dir = ctx.bin_dir.path

    has_flat = bool(ctx.file.spec)
    tree_attrs = [ctx.attr.tree_config, ctx.attr.tree_modules, ctx.attr.tree_vendor_marks]
    tree_set = len([s for s in tree_attrs if s])

    if has_flat and tree_set:
        fail("pass either spec or tree_config/tree_modules/tree_vendor_marks, not both")
    if not has_flat and tree_set == 0:
        fail("one of spec or tree_config/tree_modules/tree_vendor_marks is required")
    if tree_set != 0 and tree_set != 3:
        fail("tree_config, tree_modules, and tree_vendor_marks must all be set together")

    # Each entry is a fully-rendered shell token (already quoted/escaped).
    argv = ["run"]
    if has_flat:
        argv += ["--spec", _shell_source_path(ctx.file.spec.path)]
    else:
        pkg = ctx.label.package
        argv += [
            "--tree-config",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_config)),
            "--tree-modules",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_modules)),
            "--tree-vendor-marks",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_vendor_marks)),
            # Source-relative paths embedded in the tree config YAML
            # (e.g. `inputs.js_list_path`) resolve against the execroot.
            "--tree-source-root",
            _shell_source_path("."),
            "--out-root",
            shell.quote(out_root),
        ]

    if ctx.attr.force:
        argv.append(shell.quote("--force"))

    for pkg_label, pkg_name in ctx.attr.package_roots.items():
        pkg_files = pkg_label[DefaultInfo].files.to_list()
        if not pkg_files:
            fail("package_roots entry {} has no files".format(pkg_name))

        # The `:dir` filegroup is a single tree artifact whose `.path`
        # already points directly at the package directory containing
        # `package.json`. Make it bin-dir-relative for the post-cd cwd.
        pkg_dir = paths.relativize(pkg_files[0].path, bin_dir)
        argv += [
            shell.quote("--package-root"),
            shell.quote("{}={}".format(pkg_name, pkg_dir)),
        ]

    inputs = depset(
        direct = [ctx.file.spec] if ctx.file.spec else [],
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.spec_tree_inputs] +
                     [dep[DefaultInfo].files for dep in ctx.attr.input_data] +
                     [pkg[DefaultInfo].files for pkg in ctx.attr.package_roots.keys()],
    )

    return struct(
        argv = argv,
        command = "\"${{OLDPWD}}/{}\" {}".format(ctx.executable.debundler.path, " ".join(argv)),
        inputs = inputs,
    )

def _profile_command(profile, profile_dir_token, debundler_command):
    common = [
        "set -euo pipefail",
        "cd \"${BAZEL_BINDIR}\"",
        "profile_dir={}".format(profile_dir_token),
        "mkdir -p \"${profile_dir}\"",
        "cat > \"${profile_dir}/command.sh\" <<'EOF'",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "cd \"${BAZEL_BINDIR}\"",
        "exec {}".format(debundler_command),
        "EOF",
        "chmod +x \"${profile_dir}/command.sh\"",
    ]

    if profile == "time":
        profile_lines = [
            "/usr/bin/time -v {} > \"${{profile_dir}}/stdout.txt\" 2> \"${{profile_dir}}/stderr_time.txt\"".format(debundler_command),
        ]
    elif profile == "perf":
        profile_lines = [
            "command -v perf >/dev/null || { echo 'perf not found on PATH' >&2; exit 127; }",
            "perf record -F 99 -e cycles:u --call-graph dwarf,8192 -o \"${{profile_dir}}/perf.data\" -- {} > \"${{profile_dir}}/stdout.txt\" 2> \"${{profile_dir}}/perf_record_stderr.txt\"".format(debundler_command),
            "perf report --stdio --input \"${profile_dir}/perf.data\" --children --sort comm,dso,symbol > \"${profile_dir}/perf_report_children.txt\"",
            "perf report --stdio --input \"${profile_dir}/perf.data\" --no-children --sort comm,dso,symbol > \"${profile_dir}/perf_report_no_children.txt\"",
            "perf script --input \"${profile_dir}/perf.data\" > \"${profile_dir}/perf_script_stacks.txt\"",
            "perf report --stdio --header-only --input \"${profile_dir}/perf.data\" > \"${profile_dir}/perf_header.txt\"",
            "perf evlist --input \"${profile_dir}/perf.data\" > \"${profile_dir}/perf_evlist.txt\"",
        ]
    elif profile == "massif_heap":
        profile_lines = [
            "command -v valgrind >/dev/null || { echo 'valgrind not found on PATH' >&2; exit 127; }",
            "valgrind --tool=massif --time-unit=ms --max-snapshots=100 --detailed-freq=1 --threshold=0.5 --massif-out-file=\"${{profile_dir}}/massif_heap.out\" {} > \"${{profile_dir}}/stdout.txt\" 2> \"${{profile_dir}}/massif_heap_stderr.txt\"".format(debundler_command),
            "if command -v ms_print >/dev/null; then ms_print \"${profile_dir}/massif_heap.out\" > \"${profile_dir}/ms_print_heap.txt\"; fi",
        ]
    elif profile == "heaptrack":
        profile_lines = [
            "command -v heaptrack >/dev/null || { echo 'heaptrack not found on PATH' >&2; exit 127; }",
            "heaptrack -o \"${{profile_dir}}/heaptrack\" {} > \"${{profile_dir}}/stdout.txt\" 2> \"${{profile_dir}}/heaptrack_stderr.txt\"".format(debundler_command),
            "if command -v heaptrack_print >/dev/null; then for heaptrack_file in \"${profile_dir}\"/heaptrack*; do [ -f \"${heaptrack_file}\" ] || continue; heaptrack_print \"${heaptrack_file}\" > \"${profile_dir}/heaptrack_print.txt\"; break; done; fi",
        ]
    else:
        fail("unsupported debundle profile mode: {}".format(profile))

    return "\n".join(common + profile_lines)

def _shell_source_path(workspace_relative):
    """Shell expression referencing a workspace-root-relative source path.

    The action cd's into `${BAZEL_BINDIR}`; source-tree files live under
    `${OLDPWD}` (= execroot). Absolute paths are passed through unchanged.
    """
    if workspace_relative.startswith("/"):
        return shell.quote(workspace_relative)
    return "\"${{OLDPWD}}/{}\"".format(workspace_relative)

_DEBUNDLE_PIPELINE_ATTRS = {
    "spec": attr.label(
        allow_single_file = True,
        doc = "Optional flat transform spec YAML. Mutually exclusive with the tree_* attrs.",
    ),
    "tree_config": attr.string(
        doc = "Package-relative path to the tree-shaped authoring config YAML.",
    ),
    "tree_modules": attr.string(
        doc = "Package-relative path to the directory containing per-module YAML files.",
    ),
    "tree_vendor_marks": attr.string(
        doc = "Package-relative path to the tree-shaped vendor marks YAML.",
    ),
    "spec_tree_inputs": attr.label_list(
        allow_files = True,
        doc = "Source-tree inputs the tree-shaped spec compiler reads (typically a filegroup globbing the spec YAMLs).",
    ),
    "debundler": attr.label(
        executable = True,
        cfg = "exec",
        mandatory = True,
        doc = "Debundler binary; must support `run` with flat transform spec or tree-shaped spec args.",
    ),
    "force": attr.bool(
        doc = "Pass --force through to debundle; output roots must still be absent or empty.",
    ),
    "input_data": attr.label_list(
        allow_files = True,
        doc = "Source-tree inputs the spec references (extracted/, snapshots/).",
    ),
    "package_roots": attr.label_keyed_string_dict(
        allow_files = True,
        doc = "Vendor package roots: label of the package's `:dir` filegroup -> package name. The first file's dirname is passed as `--package-root <name>=<dir>`.",
    ),
}

def _debundle_profile_attrs():
    attrs = dict(_DEBUNDLE_PIPELINE_ATTRS)
    attrs.update({
        "profile": attr.string(
            default = "perf",
            values = ["time", "perf", "massif_heap", "heaptrack"],
            doc = "Local profiling wrapper to run around the debundle action command.",
        ),
    })
    return attrs

debundle_pipeline = rule(
    implementation = _debundle_pipeline_impl,
    attrs = _DEBUNDLE_PIPELINE_ATTRS,
)

debundle_pipeline_profile = rule(
    implementation = _debundle_pipeline_profile_impl,
    attrs = _debundle_profile_attrs(),
)

def debundle_pipeline_with_profiles(
        name,
        profile_modes = ("time", "perf", "massif_heap", "heaptrack"),
        **kwargs):
    """Create a debundle pipeline plus local profiling sibling targets.

    The regular target keeps `name`. For each entry in `profile_modes`, this
    creates `<name>_profile_<mode>` with the same inputs, package roots, cwd,
    and debundler command, but with a local profiling wrapper and a `.profile`
    tree output.
    """

    debundle_pipeline(
        name = name,
        **kwargs
    )

    for mode in profile_modes:
        profile_kwargs = dict(kwargs)
        profile_tags = list(profile_kwargs.get("tags", []))
        if "manual" not in profile_tags:
            profile_tags.append("manual")
        profile_kwargs["tags"] = profile_tags
        debundle_pipeline_profile(
            name = "{}_profile_{}".format(name, mode),
            profile = mode,
            **profile_kwargs
        )
