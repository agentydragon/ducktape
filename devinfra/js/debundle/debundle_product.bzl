"""Standard Debundle target family for one tree-shaped corpus.

`debundle_product` composes `debundle_pipeline` and `debundle_cli`; it adds
no pipeline or launcher behavior of its own. Target names are fixed by role so
every corpus exposes the same family (<docs/bazel_integration.md>
§ Standard target family for a corpus).
"""

load("@bazel_skylib//lib:paths.bzl", "paths")
load(":debundle_cli.bzl", "debundle_cli")
load(":pipeline.bzl", "debundle_pipeline")

def debundle_spec(dir, config = None, modules = None, vendor_marks = None, srcs = None):
    """Locates a tree-shaped spec: one directory with conventional file names.

    Paths are package-relative. Overrides cover corpora whose files live
    elsewhere; `config` and `vendor_marks` may also be labels of generated files.

    Args:
      dir: Spec directory; also the launchers' `spec_root`.
      config: Tree config; defaults to `<dir>/spec_config.yaml`.
      modules: Per-module YAML directory; defaults to `<dir>/modules`.
      vendor_marks: Vendor marks; defaults to `<dir>/sources/vendor/vendor_marks.yaml`.
      srcs: Spec tree inputs; defaults to every `.yaml` under `dir` and `modules`.

    Returns:
      A spec location for `debundle_product`.
    """
    return struct(
        dir = dir,
        config = config if config else paths.join(dir, "spec_config.yaml"),
        modules = modules if modules else paths.join(dir, "modules"),
        vendor_marks = vendor_marks if vendor_marks else paths.join(dir, "sources", "vendor", "vendor_marks.yaml"),
        srcs = srcs,
    )

def _spec_location(spec):
    return debundle_spec(spec) if type(spec) == "string" else spec

def _spec_srcs(spec, nested):
    if spec.srcs != None:
        return spec.srcs

    # A spec nested under another one (a residual baseline under `spec/`) is not
    # part of the outer spec's inputs.
    return native.glob(
        [paths.join(spec.dir, "**", "*.yaml"), paths.join(spec.modules, "**", "*.yaml")],
        exclude = [paths.join(other.dir, "**") for other in nested if other.dir.startswith(spec.dir + "/")],
    )

def debundle_product(
        name,
        spec,
        source,
        input_data = [],
        package_roots = {},
        readable = False,
        ownership_subset = None,
        residual_baseline = None,
        residual_baseline_reuses_analysis = False,
        cli_main_chunk = None,
        cli_chunks = [],
        cli_chunk_graphs = {},
        cli_source_root = None,
        cli_source_data = None,
        cli_graph_source_data = None,
        analysis_threads = 0,
        exec_properties = None,
        visibility = None,
        tags = None,
        testonly = None):
    """Declares the standard Debundle targets for one corpus.

    Every variant reads the same `source`, `input_data` and `package_roots`.
    Variants that share the spec reuse `<name>_analysis` instead of
    registering their own projection and Analyze actions.

    Args:
      name: Gated pipeline target; prefix of every other target.
      spec: Spec directory (string) or `debundle_spec(...)`.
      source: Target holding the chunk the spec reads (`tree_source_root`);
        need not be repeated in `input_data`.
      input_data: Further inputs the spec references.
      package_roots: Vendor package `:dir` targets mapped to package names;
        shared by pipelines and launchers.
      readable: Declare `<name>_readable` (`emit_on_rejection`).
      ownership_subset: Retained chunk/module JSON; declares `<name>_passing`.
      residual_baseline: Residual baseline spec directory or `debundle_spec(...)`;
        declares `<name>_residual_baseline` and `<name>_residual_baseline_analysis`.
      residual_baseline_reuses_analysis: The baseline reuses `<name>_analysis`
        instead of analyzing its own projection.
      cli_main_chunk: Main chunk id; declares the `<name>_cli` launcher. Required
        by every other `cli_*` argument.
      cli_chunks: Further chunk ids; each declares `<name>_cli_<chunk>`.
      cli_chunk_graphs: Launcher chunk -> chunk whose graph it reads, for
        entries of `cli_chunks` that read another chunk's graph.
      cli_source_root: `debundle_cli` `source_root`.
      cli_source_data: `debundle_cli` `source_data`.
      cli_graph_source_data: `debundle_cli` `graph_source_data`.
      analysis_threads: `analysis_threads` of each target that runs Analyze.
      exec_properties: Execution properties of every pipeline target.
      visibility: Visibility of every declared target.
      tags: Tags of every declared target.
      testonly: Test-only setting of every declared target.
    """
    spec = _spec_location(spec)
    baseline = _spec_location(residual_baseline) if residual_baseline else None
    if residual_baseline_reuses_analysis and not baseline:
        fail("residual_baseline_reuses_analysis requires residual_baseline")

    common = {}
    if visibility != None:
        common["visibility"] = visibility
    if tags != None:
        common["tags"] = tags
    if testonly != None:
        common["testonly"] = testonly
    pipeline_common = dict(common)
    if exec_properties != None:
        pipeline_common["exec_properties"] = exec_properties
    analysis = ":{}_analysis".format(name)

    def pipeline(target, spec, **kwargs):
        debundle_pipeline(
            name = target,
            input_data = input_data,
            package_roots = package_roots,
            spec_tree_inputs = _spec_srcs(spec, [baseline] if baseline else []),
            tree_config = spec.config,
            tree_modules = spec.modules,
            tree_source_root = source,
            tree_vendor_marks = spec.vendor_marks,
            **dict(pipeline_common, **kwargs)
        )

    def output_group(target, src, group):
        native.filegroup(name = target, srcs = [src], output_group = group, **common)

    pipeline(name, spec, analysis_threads = analysis_threads, split_dependency_graphs = True)
    output_group(name + "_analysis", ":" + name, "analysis")
    output_group(name + "_graphs", ":" + name, "dependency_graphs")
    if readable:
        pipeline(name + "_readable", spec, analysis = analysis, emit_on_rejection = True)
    if ownership_subset:
        pipeline(name + "_passing", spec, analysis = analysis, ownership_subset = ownership_subset)
    if baseline:
        if residual_baseline_reuses_analysis:
            pipeline(name + "_residual_baseline", baseline, analysis = analysis)
        else:
            pipeline(name + "_residual_baseline", baseline, analysis_threads = analysis_threads)
        output_group(name + "_residual_baseline_analysis", ":{}_residual_baseline".format(name), "analysis")

    _launchers(
        name = name,
        spec = spec,
        main_chunk = cli_main_chunk,
        chunks = cli_chunks,
        chunk_graphs = cli_chunk_graphs,
        source_root = cli_source_root,
        source_data = cli_source_data,
        graph_source_data = cli_graph_source_data,
        package_roots = package_roots,
        common = common,
    )

def _launchers(name, spec, main_chunk, chunks, chunk_graphs, source_root, source_data, graph_source_data, package_roots, common):
    if not main_chunk:
        if chunks or chunk_graphs or source_root or source_data or graph_source_data:
            fail("cli_* arguments require cli_main_chunk")
        return
    if main_chunk in chunks:
        fail("cli_chunks must not repeat cli_main_chunk {!r}: it is {}_cli".format(main_chunk, name))
    for chunk in chunk_graphs:
        if chunk not in chunks:
            fail("cli_chunk_graphs key {!r} is not in cli_chunks".format(chunk))

    package = native.package_name()
    for target, chunk in [(name + "_cli", main_chunk)] + [
        ("{}_cli_{}".format(name, chunk), chunk_graphs.get(chunk, chunk))
        for chunk in chunks
    ]:
        debundle_cli(
            name = target,
            debundle = ":" + name,
            graph_relpath = paths.join("reports", "tree", chunk, "dependency_graph.json.gz"),
            modules_path = paths.join(package, spec.modules),
            spec_root = paths.join(package, spec.dir),
            source_root = source_root,
            source_data = source_data,
            graph_source_data = graph_source_data,
            package_roots = package_roots,
            **common
        )
