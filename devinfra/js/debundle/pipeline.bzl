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
        "apply_projection": "Apply projection from the same authored spec.",
        "spec_identity": "Exact spec path and declared spec input identity, excluding output root.",
        "projector": "The executable that projects semantic analysis inputs.",
    },
)

def _debundle_apply_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.label.name + ".out")
    plan = _spec_plan(ctx, ctx.attr.package_roots)

    saved = ctx.attr.analysis[DebundleAnalysisInfo]
    analysis = saved.analysis

    output_groups = {
        "analysis": saved.files,
        "debundler": depset([ctx.executable.debundler]),
        "projector": depset([saved.projector]),
        "native_inputs": depset(ctx.files.ownership_subset, transitive = [plan.inputs]),
    }
    if plan.spec_identity == saved.spec_identity:
        projection = saved.apply_projection
    else:
        projection = ctx.actions.declare_file(ctx.label.name + ".apply_projection")
        _run_debundler(
            ctx,
            # A different authored spec may still share the saved semantic
            # analysis. Its apply inputs must be projected independently.
            inputs = plan.spec_inputs,
            outputs = [projection],
            args = ["spec", "project-apply"] + _spec_args(ctx, plan, out_dir.short_path) +
                   ["--projection-root", "\"${OLDPWD}\"", "--out", shell.quote(projection.short_path)],
            mnemonic = "DebundleProjectApply",
            progress = "Projecting debundle apply inputs for %{label}",
        )
    output_groups["apply_projection"] = depset([projection])
    _run_apply(ctx, plan, analysis, out_dir, projection = projection)
    if ctx.attr.split_dependency_graphs:
        # Only the output group requests the full graph export.
        graphs_dir = ctx.actions.declare_directory(ctx.label.name + ".dependency_graphs")
        _run_apply(ctx, plan, analysis, graphs_dir)
        output_groups["dependency_graphs"] = depset([graphs_dir])
    return [DefaultInfo(files = depset([out_dir])), OutputGroupInfo(**output_groups)]

def _resolve_placement_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    plan = _spec_plan(ctx, {})
    subset = ctx.file.ownership_subset
    canonical = ctx.outputs.canonical_out
    if canonical and not subset:
        fail("canonical_out requires ownership_subset")
    fragment_files = []
    extra_args = []
    for target, owner in ctx.attr.proposal_fragments.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("proposal fragment {} must supply exactly one file".format(target.label))
        fragment_files.append(files[0])

        # _run_debundler starts in the bin tree, not the execroot.
        extra_args += ["--proposal-fragment", shell.quote(owner + "=") + _shell_execroot_path(files[0].path)]
    if canonical:
        extra_args += ["--canonical-out", shell.quote(canonical.short_path)]
    _run_debundler(
        ctx,
        inputs = depset(ctx.files.ownership_subset + fragment_files, transitive = [plan.inputs]),
        outputs = [out] + ([canonical] if canonical else []),
        tool = ctx.executable.resolver,
        args = _spec_args(ctx, plan, ctx.label.name + ".unused") +
               ["--chunk", shell.quote(ctx.attr.chunk), "--out", shell.quote(out.short_path)] +
               (["--ownership-subset", _shell_execroot_path(subset.path)] if subset else []) + extra_args,
        mnemonic = "DebundleResolvePlacement",
        progress = "Resolving authored source-unit owners for %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

def _debundle_analysis_impl(ctx):
    if ctx.attr.analysis_threads < 0:
        fail("analysis_threads must be nonnegative, got {}".format(ctx.attr.analysis_threads))
    plan = _spec_plan(ctx, ctx.attr.package_roots)

    # Analysis is computed by its own action, keyed only by what it reads:
    # the projection, the sources and the package roots. An edit the
    # projection does not cover (module assignment, renames, notes,
    # `commuting_statements`) leaves the projection's bytes unchanged, so the
    # `DebundleAnalyze` action hits the action cache and only
    # `DebundleApply` reruns.
    projection = ctx.actions.declare_file(ctx.label.name + ".analysis_projection")
    apply_projection = ctx.actions.declare_file(ctx.label.name + ".apply_projection")
    analysis = ctx.actions.declare_file(ctx.label.name + ".analysis")
    _run_debundler(
        ctx,
        inputs = plan.inputs,
        outputs = [projection, apply_projection],
        args = _spec_args(ctx, plan, ctx.label.name + ".analysis_output") + plan.package_args + [
            "--out",
            shell.quote(projection.short_path),
            "--apply-out",
            shell.quote(apply_projection.short_path),
            "--projection-root",
            "\"${OLDPWD}\"",
        ],
        mnemonic = "DebundleProjectAnalysis",
        progress = "Projecting debundle analysis inputs for %{label}",
        tool = ctx.executable.projector,
    )
    _run_debundler(
        ctx,
        inputs = depset([projection], transitive = [plan.source_inputs]),
        outputs = [analysis],
        args = [
            "--projection",
            shell.quote(projection.short_path),
            "--tree-source-root",
            plan.source_root,
        ] + plan.package_args + ["--out", shell.quote(analysis.short_path)],
        mnemonic = "DebundleAnalyze",
        progress = "Analyzing debundle input for %{label}",
        tool = ctx.executable.analyzer,
        analysis_threads = ctx.attr.analysis_threads,
    )

    files = depset([projection, analysis])
    return [
        DefaultInfo(files = files),
        DebundleAnalysisInfo(
            analysis = analysis,
            files = files,
            apply_projection = apply_projection,
            spec_identity = plan.spec_identity,
            projector = ctx.executable.projector,
        ),
        OutputGroupInfo(
            analysis = files,
            projection = depset([projection]),
            apply_projection = depset([apply_projection]),
        ),
    ]

def _run_apply(ctx, plan, analysis, out_dir, projection = None):
    graph_export = projection == None
    run_flags = _run_flags(ctx, include_full_streams = not graph_export)
    if ctx.file.ownership_subset:
        run_flags += ["--ownership-subset", _shell_execroot_path(ctx.file.ownership_subset.path)]
    if graph_export:
        mnemonic = "DebundleExportGraphs"
        progress = "Exporting full debundle graphs for %{label}"
        if not ctx.attr.emit_on_rejection:
            run_flags.append("--emit-on-rejection")
        inputs = depset([analysis] + ctx.files.ownership_subset, transitive = [plan.inputs])
        extra_flags = ["--graphs", "--reports-only", shell.quote(out_dir.short_path)]
    else:
        mnemonic = "DebundleApply"
        progress = "Applying debundle spec for %{label}"
        inputs = depset([analysis, projection] + ctx.files.ownership_subset, transitive = [plan.source_inputs])
        extra_flags = ["--apply-projection", shell.quote(projection.short_path), "--projection-root", "\"${OLDPWD}\""]
    _run_debundler(
        ctx,
        inputs = inputs,
        outputs = [out_dir],
        args = ["run"] + run_flags + _spec_args(ctx, plan, out_dir.short_path) + plan.package_args +
               ["--analysis", shell.quote(analysis.short_path)] + extra_flags,
        mnemonic = mnemonic,
        progress = progress,
        gate_marker = paths.join(out_dir.short_path, "GATE_REJECTED") if ctx.attr.emit_on_rejection or graph_export else None,
    )

def _run_debundler(ctx, inputs, outputs, args, mnemonic, progress, tool = None, analysis_threads = 0, gate_marker = None):
    tool = tool or ctx.executable.debundler
    command = _shell_execroot_path(tool.path) + " " + " ".join(args)
    if gate_marker:
        # Reading output must have both the typed status and its marker.
        command += " || {{ status=$?; [ \"$status\" -eq {gate} ] && [ -f {marker} ] || exit \"$status\"; }}".format(
            gate = _GATE_REJECTED_EXIT_CODE,
            marker = shell.quote(gate_marker),
        )
    action_env = {"BAZEL_BINDIR": ctx.bin_dir.path}
    if analysis_threads > 0:
        action_env["RAYON_NUM_THREADS"] = str(analysis_threads)
    ctx.actions.run_shell(
        inputs = inputs,
        tools = [tool],
        outputs = outputs,
        command = "cd \"${{BAZEL_BINDIR}}\" && {command}".format(command = command),
        env = action_env,
        use_default_shell_env = True,
        # Local sandbox symlinks can put valid vendor files outside their
        # canonical package root. Reproducer and criteria for restoring sandboxing:
        # debug/2026_11_05_bazel_sandbox_package_roots.md.
        execution_requirements = {"no-sandbox": "1"},
        progress_message = progress,
        mnemonic = mnemonic,
    )

def _run_flags(ctx, include_full_streams):
    # Each entry is a fully-rendered shell token (already quoted/escaped).
    run_flags = []
    if ctx.attr.fail_fast:
        run_flags.append("--fail-fast")
    if ctx.attr.emit_on_rejection:
        run_flags.append("--emit-on-rejection")
    if ctx.attr.full_report_chunks and not ctx.attr.full_reports:
        fail("full_report_chunks requires full_reports")
    if not include_full_streams:
        return run_flags
    for report in ctx.attr.full_reports:
        run_flags += ["--full-report", shell.quote(report)]
    for chunk in ctx.attr.full_report_chunks:
        run_flags += ["--full-report-chunk", shell.quote(chunk)]
    if ctx.attr.effect_conflicts_jsonl:
        run_flags.append("--effect-conflicts-jsonl")
    for chunk in ctx.attr.effect_conflicts_chunks:
        run_flags.extend(["--effect-conflicts-chunk", shell.quote(chunk)])
    return run_flags

def _spec_args(ctx, plan, out_root):
    if ctx.file.spec:
        return plan.spec_args
    return plan.spec_args + ["--out-root", shell.quote(out_root)]

def _spec_plan(ctx, package_roots):
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
        spec_args = ["--spec", _shell_execroot_path(ctx.file.spec.path)]

        # A flat spec's source paths resolve against the working directory.
        source_root = "\"${PWD}\""
    else:
        pkg = ctx.label.package
        source_root = _tree_source_root_arg(ctx)
        spec_args = [
            "--tree-config",
            _shell_execroot_path(ctx.file.tree_config.path),
            "--tree-modules",
            _shell_execroot_path(paths.join(pkg, ctx.attr.tree_modules)),
            "--tree-vendor-marks",
            _shell_execroot_path(ctx.file.tree_vendor_marks.path),
            # Source-relative paths embedded in the tree config YAML
            # (e.g. `inputs.js_list_path`) resolve against this root.
            "--tree-source-root",
            source_root,
        ]

    package_args = []
    for pkg_label, pkg_name in package_roots.items():
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
                     [pkg[DefaultInfo].files for pkg in package_roots.keys()] +
                     [ctx.attr.tree_source_root[DefaultInfo].files],
    )
    spec_inputs = depset(
        direct = [ctx.file.spec] if ctx.file.spec else [ctx.file.tree_config, ctx.file.tree_vendor_marks],
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.spec_tree_inputs],
    )
    inputs = depset(
        transitive = [spec_inputs, source_inputs],
    )

    return struct(
        spec_args = spec_args,
        package_args = package_args,
        source_root = source_root,
        source_inputs = source_inputs,
        spec_inputs = spec_inputs,
        spec_identity = (
            tuple(spec_args),
            tuple(sorted([file.path for file in spec_inputs.to_list()])),
        ),
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

    # Source files have an empty root path: their root is the execroot itself.
    return _shell_execroot_path(files[0].root.path or ".")

def _shell_execroot_path(path):
    """Quote a path for the action shell after its cd from the execroot to bazel-bin."""
    if path.startswith("/"):
        return shell.quote(path)
    return '"${OLDPWD}"/' + shell.quote(path)

_SPEC_ATTRS = {
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
}

_COMMON_ATTRS = dict(_SPEC_ATTRS, **{
    "package_roots": attr.label_keyed_string_dict(
        allow_files = True,
        doc = "Vendor package roots: label of the package's `:dir` filegroup -> package name. The first file's dirname is passed as `--package-root <name>=<dir>`.",
    ),
})

debundle_resolve_placement = rule(
    implementation = _resolve_placement_impl,
    attrs = dict(_SPEC_ATTRS, **{
        "canonical_out": attr.output(doc = "Optional unfiltered JSON from the same solve as ownership_subset."),
        "proposal_fragments": attr.label_keyed_string_dict(
            allow_files = [".yaml"],
            doc = "Selector-only YAML files mapped to canonical owners in chunk. New owners follow the shared priority.",
        ),
        "chunk": attr.string(mandatory = True),
        "resolver": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//devinfra/js/debundle:debundle_resolve_placement"),
        ),
        "ownership_subset": attr.label(allow_single_file = [".json"]),
    }),
    doc = "Export selector-resolved unit ownership for one chunk, before analysis, scheduling, gates or emission.",
)

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
            doc = "Optional retained chunk/module list for normal emission and provenance-bound graph export with full spec context. Requires inline_in_entry, per_atomic_group, or coarse_atomic_groups.",
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
            doc = "Complete diagnostic sinks to export (translation_validation, export_reentry, residual_atomic_groups). Independent of full graphs; only Pipeline inputs change.",
        ),
        "full_report_chunks": attr.string_list(
            doc = "Select only these full-report sinks; checks and bounded summaries still cover every configured chunk. Requires full_reports.",
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
