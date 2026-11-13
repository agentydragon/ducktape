"""Explicit Debundle stages: analyze one input, then apply one or more specs.

`debundle_analysis` registers projection and source-analysis actions only.
`debundle_apply` requires that target's typed provider and registers assignment,
gates, translation validation and emission only. Declare a shared analysis target
once for variants with the same semantic analysis projection; distinct source or
analysis premises require a distinct target. Native compatibility checks remain
mandatory. Full dependency-graph export is an optional apply-stage output group,
because authored graph options and module ownership belong to that stage.
"""

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//lib:shell.bzl", "shell")

# Sync: `GATE_REJECTED_EXIT_CODE` in `pipeline.rs`.
_GATE_REJECTED_EXIT_CODE = 3

DebundleAnalysisInfo = provider(
    doc = "Saved source facts and their canonical semantic input projection.",
    fields = {
        "analysis": "The saved .analysis artifact.",
        "files": "Projection and saved analysis files.",
        "projector": "The executable that projects semantic analysis inputs.",
    },
)

def _debundle_apply_impl(ctx):
    if ctx.file.ownership_subset and ctx.attr.split_dependency_graphs:
        fail("ownership_subset does not support split_dependency_graphs")
    out_dir = ctx.actions.declare_directory(ctx.label.name + ".out")
    plan = _spec_plan(ctx, out_dir.short_path)

    saved = ctx.attr.analysis[DebundleAnalysisInfo]
    analysis_files = saved.files
    analysis = saved.analysis

    output_groups = {
        "analysis": analysis_files,
        "debundler": depset([ctx.executable.debundler]),
        "projector": depset([saved.projector]),
        "native_inputs": depset(ctx.files.ownership_subset, transitive = [plan.inputs]),
    }
    _run_debundler(
        ctx,
        inputs = depset([analysis] + ctx.files.ownership_subset, transitive = [plan.inputs]),
        outputs = [out_dir],
        command = _apply_command(ctx, plan, analysis, []),
        mnemonic = "DebundleApply",
        progress = "Applying debundle spec for %{label}",
    )
    if ctx.attr.split_dependency_graphs:
        # Register an independent, on-demand export. DefaultInfo never reaches
        # this action; it consumes the exact SAME Analyze artifact as Pipeline.
        graphs_dir = ctx.actions.declare_directory(ctx.label.name + ".dependency_graphs")
        graph_plan = _spec_plan(ctx, graphs_dir.short_path)
        _run_debundler(
            ctx,
            inputs = depset([analysis], transitive = [graph_plan.inputs]),
            outputs = [graphs_dir],
            command = _apply_command(ctx, graph_plan, analysis, [
                "--graphs",
                "--reports-only",
                shell.quote(graphs_dir.short_path),
            ], allow_gate_rejection = True),
            mnemonic = "DebundleExportGraphs",
            progress = "Exporting full debundle graphs for %{label}",
        )
        output_groups["dependency_graphs"] = depset([graphs_dir])
    return [DefaultInfo(files = depset([out_dir])), OutputGroupInfo(**output_groups)]

def _debundle_analysis_impl(ctx):
    if ctx.attr.analysis_threads < 0:
        fail("analysis_threads must be nonnegative, got {}".format(ctx.attr.analysis_threads))
    plan = _spec_plan(ctx, ctx.label.name + ".analysis_output")

    # Analysis is computed by its own action, keyed only by what it reads:
    # the projection, the sources and the package roots. An edit the
    # projection does not cover (module assignment, renames, notes,
    # `commuting_statements`) leaves the projection's bytes unchanged, so the
    # `DebundleAnalyze` action hits the action cache and only
    # `DebundleApply` reruns.
    projection = ctx.actions.declare_file(ctx.label.name + ".analysis_projection")
    analysis = ctx.actions.declare_file(ctx.label.name + ".analysis")
    _run_debundler(
        ctx,
        inputs = plan.inputs,
        outputs = [projection],
        command = _shell_execroot_path(ctx.executable.projector.path) + " " + " ".join(
            plan.spec_args + plan.package_args +
            ["--out", shell.quote(projection.short_path)],
        ),
        mnemonic = "DebundleProjectAnalysis",
        progress = "Projecting debundle analysis inputs for %{label}",
        tool = ctx.executable.projector,
    )
    _run_debundler(
        ctx,
        inputs = depset([projection], transitive = [plan.source_inputs]),
        outputs = [analysis],
        command = "\"${{OLDPWD}}/{}\" ".format(ctx.executable.analyzer.path) + " ".join(
            [
                "--projection",
                shell.quote(projection.short_path),
                "--tree-source-root",
                plan.source_root,
            ] + plan.package_args + ["--out", shell.quote(analysis.short_path)],
        ),
        mnemonic = "DebundleAnalyze",
        progress = "Analyzing debundle input for %{label}",
        tool = ctx.executable.analyzer,
        analysis_threads = ctx.attr.analysis_threads,
    )

    files = depset([projection, analysis])
    return [
        DefaultInfo(files = files),
        DebundleAnalysisInfo(analysis = analysis, files = files, projector = ctx.executable.projector),
        OutputGroupInfo(analysis = files, projection = depset([projection])),
    ]

def _apply_command(ctx, plan, analysis, extra_flags, allow_gate_rejection = False):
    run_flags = _run_flags(ctx, include_full_streams = "--reports-only" not in extra_flags)
    if ctx.file.ownership_subset:
        run_flags = run_flags + ["--ownership-subset", _shell_source_path(ctx.file.ownership_subset.path)]
    if allow_gate_rejection and not ctx.attr.emit_on_rejection:
        run_flags = run_flags + ["--emit-on-rejection"]
    command = "\"${{OLDPWD}}/{}\" ".format(ctx.executable.debundler.path) + " ".join(
        ["run"] + run_flags + plan.spec_args + plan.package_args +
        ["--analysis", shell.quote(analysis.short_path)] + extra_flags,
    )
    if ctx.attr.emit_on_rejection or allow_gate_rejection:
        # Reading output must have both the typed status and its marker.
        return "{command} || {{ status=$?; [ \"$status\" -eq {gate} ] && [ -f {marker} ] || exit \"$status\"; }}".format(
            command = command,
            gate = _GATE_REJECTED_EXIT_CODE,
            marker = shell.quote(paths.join(plan.out_root, "GATE_REJECTED")),
        )
    return command

def _run_debundler(ctx, inputs, outputs, command, mnemonic, progress, tool = None, analysis_threads = 0):
    action_env = {"BAZEL_BINDIR": ctx.bin_dir.path}
    if analysis_threads > 0:
        action_env["RAYON_NUM_THREADS"] = str(analysis_threads)
    ctx.actions.run_shell(
        inputs = inputs,
        tools = [tool if tool else ctx.executable.debundler],
        outputs = outputs,
        command = "cd \"${{BAZEL_BINDIR}}\" && {command}".format(command = command),
        env = action_env,
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

def _run_flags(ctx, include_full_streams = True):
    # Each entry is a fully-rendered shell token (already quoted/escaped).
    run_flags = []
    if ctx.attr.fail_fast:
        run_flags.append("--fail-fast")
    if ctx.attr.emit_on_rejection:
        run_flags.append("--emit-on-rejection")
    if ctx.attr.full_report_chunks and not ctx.attr.full_reports:
        fail("full_report_chunks requires full_reports")
    if ctx.attr.residual_extraction_atoms and "residual_extractions" not in ctx.attr.full_reports:
        fail("residual_extraction_atoms requires residual_extractions in full_reports")
    for report in ctx.attr.full_reports if include_full_streams else []:
        run_flags += ["--full-report", shell.quote(report)]
    for chunk in ctx.attr.full_report_chunks if include_full_streams else []:
        run_flags += ["--full-report-chunk", shell.quote(chunk)]
    for atom in ctx.attr.residual_extraction_atoms if include_full_streams else []:
        run_flags += ["--residual-extraction-atom", shell.quote(atom)]
    if include_full_streams and ctx.attr.effect_conflicts_jsonl:
        run_flags.append("--effect-conflicts-jsonl")
    for chunk in ctx.attr.effect_conflicts_chunks if include_full_streams else []:
        run_flags.extend(["--effect-conflicts-chunk", shell.quote(chunk)])
    return run_flags

def _spec_plan(ctx, out_root):
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

    if has_flat:
        spec_args = ["--spec", _shell_source_path(ctx.file.spec.path)]

        # A flat spec's source paths resolve against the working directory.
        source_root = "\"${PWD}\""
    else:
        pkg = ctx.label.package
        source_root = _tree_source_root_arg(ctx)
        spec_args = [
            "--tree-config",
            _shell_source_path(ctx.file.tree_config.path),
            "--tree-modules",
            _shell_source_path(paths.join(pkg, ctx.attr.tree_modules)),
            "--tree-vendor-marks",
            _shell_source_path(ctx.file.tree_vendor_marks.path),
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
        direct = [ctx.file.spec] if ctx.file.spec else [ctx.file.tree_config, ctx.file.tree_vendor_marks],
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.spec_tree_inputs] +
                     [source_inputs],
    )

    return struct(
        spec_args = spec_args,
        package_args = package_args,
        source_root = source_root,
        source_inputs = source_inputs,
        inputs = inputs,
        out_root = out_root,
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

_COMMON_ATTRS = {
    "spec": attr.label(
        allow_single_file = True,
        doc = "Optional flat transform spec YAML. Mutually exclusive with the tree_* attrs.",
    ),
    "tree_config": attr.label(
        allow_single_file = True,
        doc = "Source or generated tree-shaped authoring config YAML.",
    ),
    "tree_modules": attr.string(
        doc = "Package-relative path to the directory containing per-module YAML files.",
    ),
    "tree_vendor_marks": attr.label(
        allow_single_file = True,
        doc = "Source or generated tree-shaped vendor marks YAML.",
    ),
    "spec_tree_inputs": attr.label_list(
        allow_files = True,
        doc = "Source-tree inputs the tree-shaped spec compiler reads (typically a filegroup globbing the spec YAMLs).",
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
}

debundle_analysis = rule(
    implementation = _debundle_analysis_impl,
    attrs = dict(_COMMON_ATTRS, **{
        "analyzer": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//devinfra/js/debundle:analyzer"),
            doc = "Analysis-only binary; custom analyzers must share the CLI's compiled analysis identity.",
        ),
        "projector": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//devinfra/js/debundle:projector"),
            doc = "Small projection binary; custom projectors must write the canonical pipeline analysis projection.",
        ),
        "analysis_threads": attr.int(
            default = 0,
            doc = "Rayon worker limit for DebundleAnalyze; 0 omits the override, using inherited RAYON_NUM_THREADS or Rayon's default, and positive values set RAYON_NUM_THREADS for that action only.",
        ),
    }),
    doc = "Project semantic analysis inputs and compute reusable source facts; never applies module ownership or emits JavaScript.",
)

debundle_apply = rule(
    implementation = _debundle_apply_impl,
    attrs = dict(_COMMON_ATTRS, **{
        "analysis": attr.label(
            mandatory = True,
            providers = [DebundleAnalysisInfo],
            doc = "Explicit debundle_analysis target. Source, engine and semantic projection compatibility are checked before using its saved facts.",
        ),
        "debundler": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//devinfra/js/debundle:debundler"),
            doc = "Debundler binary; must support `run` with flat transform spec or tree-shaped spec args.",
        ),
        "ownership_subset": attr.label(
            allow_single_file = [".json"],
            doc = "Optional retained chunk/module list for normal emission with full spec context. Only the Pipeline action reads it; requires inline_in_entry, per_atomic_group, or coarse_atomic_groups and conflicts with split_dependency_graphs.",
        ),
        "emit_on_rejection": attr.bool(
            default = False,
            doc = (
                "Pass --emit-on-rejection and succeed when the realizability gate's " +
                "rejection is the run's only failure: the output is then a " +
                "reading-only tree marked by a `GATE_REJECTED` file at its " +
                "`write_js_tree` root, never a runnable bundle. Name such a target " +
                "for that (for example, `<name>_readable`)."
            ),
        ),
        "split_dependency_graphs": attr.bool(
            default = False,
            doc = (
                "Expose full graphs as an on-demand dependency_graphs output group " +
                "(`<name>.dependency_graphs/`, same report paths), using the same " +
                "Analyze artifact. Default outputs never generate full graphs."
            ),
        ),
        "full_reports": attr.string_list(
            doc = "Complete diagnostic sinks to export (translation_validation, export_reentry, residual_atomic_groups, residual_extractions). Independent of full graphs; only Pipeline inputs change.",
        ),
        "full_report_chunks": attr.string_list(
            doc = "Select only these full-report sinks; checks and bounded summaries still cover every configured chunk. Requires full_reports.",
        ),
        "residual_extraction_atoms": attr.string_list(
            doc = "Probe only these atomic IDs in residual_extractions. Other residual groups are listed as unprobed.",
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
    }),
    doc = "Apply a spec to explicit saved source facts, checking gates and translation before emitting JavaScript. Never registers an Analyze action.",
)
