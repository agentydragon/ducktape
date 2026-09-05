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
    selector_request_proto = ctx.actions.declare_file(ctx.label.name + ".selector_cpsat_request.pb")
    selector_problem_log = ctx.actions.declare_file(ctx.label.name + ".selector_problem.log")
    selector_problem_scratch = ctx.actions.declare_directory(ctx.label.name + ".selector_problem.out")
    bin_dir = ctx.bin_dir.path
    plan = _debundle_pipeline_plan(ctx, out_dir.short_path)
    selector_problem_plan = _debundle_pipeline_plan(ctx, selector_problem_scratch.short_path)
    tools = [
        ctx.executable.debundler,
        ctx.executable.ortools_cpsat_solver,
    ]
    exec_env = _selector_solver_env(
        ctx,
        request_proto_dir = _shell_execroot_path(paths.join(out_dir.path, "debug/selector_cpsat_requests")),
        summary_json_dir = _shell_execroot_path(paths.join(out_dir.path, "debug/selector_cpsat_summaries")),
    )

    ctx.actions.run_shell(
        inputs = plan.inputs,
        tools = tools,
        outputs = [out_dir],
        command = "cd \"${{BAZEL_BINDIR}}\" && {exec_env}exec {command}".format(
            exec_env = exec_env,
            command = plan.command,
        ),
        env = {"BAZEL_BINDIR": bin_dir},
        use_default_shell_env = True,
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

    selector_problem_env = _selector_solver_env(
        ctx,
        _shell_execroot_path(selector_request_proto.path),
        dump_only = True,
    )
    selector_request_proto_path = _shell_execroot_path(selector_request_proto.path)
    selector_problem_scratch_path = _shell_execroot_path(selector_problem_scratch.path)
    selector_problem_log_path = _shell_execroot_path(selector_problem_log.path)
    ctx.actions.run_shell(
        inputs = selector_problem_plan.inputs,
        tools = tools,
        outputs = [
            selector_request_proto,
            selector_problem_log,
            selector_problem_scratch,
        ],
        command = """
cd "${{BAZEL_BINDIR}}"
mkdir -p {selector_problem_scratch}
({exec_env}{command}) > {log} 2>&1 || true
if test -s {request_proto}; then
  exit 0
fi
cat {log} >&2
if ! test -s {request_proto}; then
  echo "selector CP-SAT request protobuf was not written: {request_proto}" >&2
fi
exit 1
""".format(
            exec_env = selector_problem_env,
            command = selector_problem_plan.command,
            log = selector_problem_log_path,
            request_proto = selector_request_proto_path,
            selector_problem_scratch = selector_problem_scratch_path,
        ),
        env = {"BAZEL_BINDIR": bin_dir},
        use_default_shell_env = True,
        execution_requirements = {"no-sandbox": "1"},
        progress_message = "Dumping selector CP-SAT request for %{label}",
        mnemonic = "DebundleSelectorProblem",
    )

    return [
        DefaultInfo(files = depset([out_dir])),
        OutputGroupInfo(selector_problem = depset([
            selector_request_proto,
            selector_problem_log,
        ])),
    ]

def _selector_solver_env(
        ctx,
        request_proto_path = None,
        summary_json_path = None,
        request_proto_dir = None,
        summary_json_dir = None,
        dump_only = False):
    exec_env = "{}={} ".format(
        "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER",
        _shell_execroot_path(ctx.executable.ortools_cpsat_solver.path),
    )
    if request_proto_path:
        exec_env += "{}={} ".format(
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO",
            request_proto_path,
        )
    if summary_json_path:
        exec_env += "{}={} ".format(
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON",
            summary_json_path,
        )
    if request_proto_dir:
        exec_env += "{}={} ".format(
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO_DIR",
            request_proto_dir,
        )
    if summary_json_dir:
        exec_env += "{}={} ".format(
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR",
            summary_json_dir,
        )
    if dump_only:
        exec_env += "{}={} ".format(
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_DUMP_ONLY",
            shell.quote("1"),
        )
    return exec_env

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
    if ctx.attr.fail_fast:
        argv.append("--fail-fast")
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
            # (e.g. `inputs.js_list_path`) resolve against this root.
            "--tree-source-root",
            _tree_source_root_arg(ctx),
            "--out-root",
            shell.quote(out_root),
        ]

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
                     [pkg[DefaultInfo].files for pkg in ctx.attr.package_roots.keys()] +
                     [ctx.attr.tree_source_root[DefaultInfo].files],
    )

    return struct(
        argv = argv,
        command = "\"${{OLDPWD}}/{}\" {}".format(ctx.executable.debundler.path, " ".join(argv)),
        inputs = inputs,
    )

def _tree_source_root_arg(ctx):
    """`--tree-source-root`: the root the tree config's source-relative
    paths (`inputs.root`, `inputs.js_list_path`) resolve against.

    Always a target — the one holding the chunk the spec reads. The root
    comes from that target's own files via `File.root.path`: empty for
    source files, so a committed chunk resolves against the execroot, and
    `bazel-out/<cfg>/bin` for generated ones, so a chunk *extracted from a
    pinned upstream artifact by a build action* resolves against bazel-bin.
    One form covers both, which is why this is not derived from
    `input_data`: that attr legitimately mixes roots (a source chunk
    alongside generated vendor bundles), and the chunk root has to be
    named unambiguously.
    """
    dep = ctx.attr.tree_source_root
    files = dep[DefaultInfo].files.to_list()
    if not files:
        fail("tree_source_root target {} produced no files".format(dep.label))

    roots = {f.root.path: None for f in files}
    if len(roots) != 1:
        fail("tree_source_root target {} spans multiple roots: {}".format(
            dep.label,
            sorted(roots),
        ))

    root = files[0].root.path
    if not root:
        # Source file: its root *is* the execroot.
        return _shell_source_path(".")
    return _shell_execroot_path(root)

def _shell_source_path(workspace_relative):
    """Shell expression referencing a workspace-root-relative source path.

    The action cd's into `${BAZEL_BINDIR}`; source-tree files live under
    `${OLDPWD}` (= execroot). Absolute paths are passed through unchanged.
    """
    if workspace_relative.startswith("/"):
        return shell.quote(workspace_relative)
    return _shell_execroot_path(workspace_relative)

def _shell_execroot_path(execroot_relative):
    """Shell expression referencing an execroot-relative path."""
    return "\"${{OLDPWD}}/{}\"".format(execroot_relative)

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
        default = Label("//devinfra/js/debundle:debundler"),
        doc = "Debundler binary; must support `run` with flat transform spec or tree-shaped spec args.",
    ),
    "ortools_cpsat_solver": attr.label(
        executable = True,
        cfg = "exec",
        default = Label("//devinfra/js/debundle:ortools_cpsat_solver"),
        doc = "OR-Tools CP-SAT selector solver used for global selector assignment.",
    ),
    "input_data": attr.label_list(
        allow_files = True,
        doc = "Inputs the spec references (extracted/, snapshots/, vendor bundles). Source-tree files, generated outputs, or a mix.",
    ),
    "tree_source_root": attr.label(
        mandatory = True,
        doc = (
            "Target holding the chunk the spec reads. Its files' own root " +
            "becomes the root that the tree config's source-relative paths " +
            "(`inputs.root`, `inputs.js_list_path`) resolve against: the " +
            "execroot for a committed chunk, bazel-bin for one produced by " +
            "a build action. One form covers both, so a corpus never has to " +
            "vendor its chunk into git to be buildable."
        ),
    ),
    "package_roots": attr.label_keyed_string_dict(
        allow_files = True,
        doc = "Vendor package roots: label of the package's `:dir` filegroup -> package name. The first file's dirname is passed as `--package-root <name>=<dir>`.",
    ),
    "fail_fast": attr.bool(
        default = False,
        doc = "Pass --fail-fast to debundle run for debugging; by default broad pipeline runs aggregate supported diagnostics.",
    ),
}

debundle_pipeline = rule(
    implementation = _debundle_pipeline_impl,
    attrs = _DEBUNDLE_PIPELINE_ATTRS,
)
