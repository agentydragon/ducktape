"""Bazel-driven debundle pipeline rule.

Runs the ducktape debundler with `--out-root` pointing at a tree-artifact
output directory. All pipeline outputs (manifests, analysis, emitted JS) land
under the rule's declared output directory in `bazel-bin/`. Each corpus may
layer a `write_source_files` regen target on top to commit a subset of outputs
into the source tree.

Three actions: `DebundleProjectAnalysis` writes what analysis reads from the
spec, `DebundleAnalyze` computes the analysis from that projection and the
sources alone, and `DebundlePipeline` runs against the stored analysis. The
`analysis` output group holds the projection and the analysis artifact.
"""

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//lib:shell.bzl", "shell")

# Sync: `GATE_REJECTED_EXIT_CODE` in `pipeline.rs`.
_GATE_REJECTED_EXIT_CODE = 3

def _debundle_pipeline_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.label.name + ".out")
    plan = _debundle_pipeline_plan(ctx, out_dir.short_path)

    # Analysis is computed by its own action, keyed only by what it reads:
    # the projection, the sources and the package roots. An edit the
    # projection does not cover (module assignment, renames, notes,
    # `commuting_statements`) leaves the projection's bytes unchanged, so the
    # `DebundleAnalyze` action hits the action cache and only
    # `DebundlePipeline` reruns.
    projection = ctx.actions.declare_file(ctx.label.name + ".analysis_projection")
    analysis = ctx.actions.declare_file(ctx.label.name + ".analysis")
    _run_debundler(
        ctx,
        inputs = plan.inputs,
        outputs = [projection],
        command = plan.debundler + " " + " ".join(
            ["spec", "project-analysis"] + plan.spec_args + plan.package_args +
            ["--out", shell.quote(projection.short_path)],
        ),
        mnemonic = "DebundleProjectAnalysis",
        progress = "Projecting debundle analysis inputs for %{label}",
    )
    _run_debundler(
        ctx,
        inputs = depset([projection], transitive = [plan.source_inputs]),
        outputs = [analysis],
        command = plan.debundler + " " + " ".join(
            [
                "analyze",
                "--projection",
                shell.quote(projection.short_path),
                "--tree-source-root",
                plan.source_root,
            ] + plan.package_args + ["--out", shell.quote(analysis.short_path)],
        ),
        mnemonic = "DebundleAnalyze",
        progress = "Analyzing debundle input for %{label}",
    )

    run = plan.debundler + " " + " ".join(
        ["run"] + plan.run_flags + plan.spec_args + plan.package_args +
        ["--analysis", shell.quote(analysis.short_path)],
    )
    if ctx.attr.emit_on_rejection:
        # Only the gate-rejected status passes; every other failure fails the action.
        run = "{command} || {{ status=$?; [ \"$status\" -eq {gate} ] || exit \"$status\"; }}".format(
            command = run,
            gate = _GATE_REJECTED_EXIT_CODE,
        )
    outputs = [out_dir]
    output_groups = {
        # The projection and analysis artifact, for a local
        # `debundle run --analysis` loop.
        "analysis": depset([projection, analysis]),
    }
    if ctx.attr.split_dependency_graphs:
        graphs_dir = ctx.actions.declare_directory(ctx.label.name + ".dependency_graphs")
        outputs.append(graphs_dir)
        output_groups["dependency_graphs"] = depset([graphs_dir])

        # Same relative path under `graphs_dir` as it had under `out_dir`.
        run = (
            "{{ {run}; }} && mkdir -p {graphs} && " +
            "find {out}/reports \\( -name dependency_graph.json.gz -o -name program_graph.json.gz -o -name dependency_graph.json -o -name program_graph.json \\) -print0 | " +
            "while IFS= read -r -d '' f; do " +
            "d={graphs}/\"${{f#{out}/}}\"; mkdir -p \"$(dirname \"$d\")\" && mv \"$f\" \"$d\" || exit 1; done"
        ).format(
            run = run,
            out = shell.quote(out_dir.short_path),
            graphs = shell.quote(graphs_dir.short_path),
        )
    _run_debundler(
        ctx,
        inputs = depset([analysis], transitive = [plan.inputs]),
        outputs = outputs,
        command = run,
        mnemonic = "DebundlePipeline",
        progress = "Running debundle pipeline for %{label}",
    )
    return [DefaultInfo(files = depset([out_dir])), OutputGroupInfo(**output_groups)]

def _run_debundler(ctx, inputs, outputs, command, mnemonic, progress):
    ctx.actions.run_shell(
        inputs = inputs,
        tools = [ctx.executable.debundler],
        outputs = outputs,
        command = "cd \"${{BAZEL_BINDIR}}\" && {command}".format(command = command),
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        use_default_shell_env = True,
        # The debundler asserts that each vendor package's resolved subpath
        # canonicalizes to a location within the package root. Inside
        # Bazel's linux-sandbox, package-dir entries are real directories
        # but their leaf files are symlinks to the host execroot's bazel-bin
        # — so `realpath(file)` lands outside `realpath(dir)` and the check
        # spuriously fails. Disable sandboxing for these actions; inputs are
        # declared via Bazel attrs, so reproducibility is preserved.
        execution_requirements = {"no-sandbox": "1"},
        progress_message = progress,
        mnemonic = mnemonic,
    )

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
    run_flags = []
    if ctx.attr.fail_fast:
        run_flags.append("--fail-fast")
    if ctx.attr.emit_on_rejection:
        run_flags.append("--emit-on-rejection")
    if ctx.attr.effect_conflicts_jsonl:
        run_flags.append("--effect-conflicts-jsonl")
    for chunk in ctx.attr.effect_conflicts_chunks:
        run_flags.extend(["--effect-conflicts-chunk", shell.quote(chunk)])
    if has_flat:
        spec_args = ["--spec", _shell_source_path(ctx.file.spec.path)]

        # A flat spec's source paths resolve against the working directory.
        source_root = "\"${PWD}\""
    else:
        pkg = ctx.label.package
        source_root = _tree_source_root_arg(ctx)
        spec_args = [
            "--tree-config",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_config)),
            "--tree-modules",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_modules)),
            "--tree-vendor-marks",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_vendor_marks)),
            # Source-relative paths embedded in the tree config YAML
            # (e.g. `inputs.js_list_path`) resolve against this root.
            "--tree-source-root",
            source_root,
            "--out-root",
            shell.quote(out_root),
        ]

    package_args = []
    for pkg_label, pkg_name in ctx.attr.package_roots.items():
        pkg_files = pkg_label[DefaultInfo].files.to_list()
        if not pkg_files:
            fail("package_roots entry {} has no files".format(pkg_name))

        # The `:dir` filegroup is a single tree artifact whose `.path`
        # already points directly at the package directory containing
        # `package.json`. Make it bin-dir-relative for the post-cd cwd.
        pkg_dir = paths.relativize(pkg_files[0].path, bin_dir)
        package_args += [
            shell.quote("--package-root"),
            shell.quote("{}={}".format(pkg_name, pkg_dir)),
        ]

    # What analysis reads besides its projection: never the spec tree.
    source_inputs = depset(
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.input_data] +
                     [pkg[DefaultInfo].files for pkg in ctx.attr.package_roots.keys()] +
                     [ctx.attr.tree_source_root[DefaultInfo].files],
    )
    inputs = depset(
        direct = [ctx.file.spec] if ctx.file.spec else [],
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.spec_tree_inputs] +
                     [source_inputs],
    )

    return struct(
        debundler = "\"${{OLDPWD}}/{}\"".format(ctx.executable.debundler.path),
        run_flags = run_flags,
        spec_args = spec_args,
        package_args = package_args,
        source_root = source_root,
        source_inputs = source_inputs,
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
    "emit_on_rejection": attr.bool(
        default = False,
        doc = (
            "Pass --emit-on-rejection and succeed when the realizability gate's " +
            "rejection is the run's only failure: the output is then a " +
            "reading-only tree marked by a `GATE_REJECTED` file at its " +
            "`write_js_tree` root, never a runnable bundle. Name such a target " +
            "for that (`debundle_readable`)."
        ),
    ),
    "split_dependency_graphs": attr.bool(
        default = False,
        doc = (
            "Move every `reports/**/dependency_graph.json.gz` and the " +
            "`program_graph.json.gz` they reference out of the default " +
            "output into the `dependency_graphs` output group " +
            "(`<name>.dependency_graphs/`, same relative paths). The graphs " +
            "dominate a large corpus's output; a reading target rarely needs " +
            "them."
        ),
    ),
    "fail_fast": attr.bool(
        default = False,
        doc = "Pass --fail-fast to debundle run for debugging; by default broad pipeline runs aggregate supported diagnostics.",
    ),
    "effect_conflicts_jsonl": attr.bool(
        default = False,
        doc = "Stream full effect-conflict JSONL into the existing reports tree; disabled by default.",
    ),
    "effect_conflicts_chunks": attr.string_list(
        default = [],
        doc = "Restrict JSONL sinks to these materialized chunk IDs; requires effect_conflicts_jsonl. Empty selects all chunks. The CLI validates IDs.",
    ),
}

debundle_pipeline = rule(
    implementation = _debundle_pipeline_impl,
    attrs = _DEBUNDLE_PIPELINE_ATTRS,
)
