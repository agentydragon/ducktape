"""Action-level controls for the public debundle pipeline's report flags."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_BaselineActions = provider(fields = ["actions"])

def _baseline_actions_impl(target, _ctx):
    return [_BaselineActions(actions = target.actions)]

_baseline_actions = aspect(implementation = _baseline_actions_impl)

def _normalize(values, owner):
    return [value.replace(owner, "pipeline") for value in values]

def _pipeline_report_flags_test_impl(ctx):
    env = analysistest.begin(ctx)
    target = analysistest.target_under_test(env)
    actions = {action.mnemonic: action for action in analysistest.target_actions(env)}
    baseline = {action.mnemonic: action for action in ctx.attr.baseline[_BaselineActions].actions}
    expected_actions = ["DebundleAnalyze", "DebundlePipeline", "DebundleProjectAnalysis"]
    if ctx.attr.readable:
        expected_actions.append("DebundleExportGraphs")
    asserts.equals(env, sorted(expected_actions), sorted(actions))
    asserts.equals(env, sorted(baseline), sorted(actions))
    for action in actions.values():
        asserts.true(env, "BAZEL_BINDIR" in action.env)
        asserts.false(env, "RAYON_NUM_THREADS" in action.env)
    for mnemonic in sorted(actions):
        actual = actions[mnemonic]
        original = baseline[mnemonic]
        argv = _normalize(actual.argv, target.label.name)
        expected = _normalize(original.argv, ctx.attr.baseline.label.name)
        if mnemonic == "DebundlePipeline" and ctx.attr.expected_flags:
            asserts.true(env, ctx.attr.expected_flags + " " in argv[-1])
            argv[-1] = argv[-1].replace(ctx.attr.expected_flags + " ", "")
        asserts.equals(env, expected, argv)
        asserts.equals(
            env,
            sorted(_normalize([file.path for file in original.inputs.to_list()], ctx.attr.baseline.label.name)),
            sorted(_normalize([file.path for file in actual.inputs.to_list()], target.label.name)),
        )
        asserts.equals(
            env,
            _normalize([file.path for file in original.outputs.to_list()], ctx.attr.baseline.label.name),
            _normalize([file.path for file in actual.outputs.to_list()], target.label.name),
        )
    command = actions["DebundlePipeline"].argv[-1]
    analysis_inputs = actions["DebundleAnalyze"].inputs.to_list()
    asserts.true(env, any([file.basename == "debundle_analyze" for file in analysis_inputs]))
    asserts.false(env, any([file.basename == "debundle" for file in analysis_inputs]))
    asserts.false(env, any([file.basename == "debundle_project_analysis" for file in analysis_inputs]))
    for mnemonic in [name for name in expected_actions if name in ["DebundlePipeline", "DebundleExportGraphs"]]:
        files = actions[mnemonic].inputs.to_list()
        asserts.true(env, any([file.basename == "debundle" for file in files]))
        asserts.false(env, any([file.basename == "debundle_analyze" for file in files]))
        asserts.false(env, any([file.basename == "debundle_project_analysis" for file in files]))
    project_inputs = actions["DebundleProjectAnalysis"].inputs.to_list()
    asserts.true(env, any([file.basename == "debundle_project_analysis" for file in project_inputs]))
    asserts.false(env, any([file.basename == "debundle" for file in project_inputs]))
    asserts.false(env, any([file.basename == "debundle_analyze" for file in project_inputs]))
    project_command = actions["DebundleProjectAnalysis"].argv[-1]
    asserts.true(env, "debundle_project_analysis" in project_command)
    asserts.false(env, "spec project-analysis" in project_command)
    asserts.false(env, "--effect-conflicts" in baseline["DebundlePipeline"].argv[-1])
    asserts.false(env, "--full-report" in baseline["DebundlePipeline"].argv[-1])
    asserts.equals(env, ctx.attr.readable, '[ "$status" -eq 3 ] && [ -f ' in command)
    if ctx.attr.readable:
        asserts.true(env, target.label.name + ".out/GATE_REJECTED" in command)

    asserts.false(env, "--graphs" in command)
    asserts.equals(env, [target.label.name + ".out"], [file.basename for file in actions["DebundlePipeline"].outputs.to_list()])

    # The graph bundle is a separate action sharing the same analysis artifact;
    # it is not reachable from DefaultInfo and enables no full JSONL detail.
    asserts.equals(
        env,
        [target.label.name + ".out"],
        [file.basename for file in target[DefaultInfo].files.to_list()],
    )
    groups = target[OutputGroupInfo]
    asserts.equals(env, 2, len(groups.analysis.to_list()))
    asserts.equals(env, ["debundle_project_analysis"], [file.basename for file in groups.projector.to_list()])
    asserts.equals(env, ["debundle"], [file.basename for file in groups.debundler.to_list()])
    analyzed = actions["DebundleAnalyze"].outputs.to_list()[0]
    native_inputs = groups.native_inputs.to_list()
    pipeline_inputs = actions["DebundlePipeline"].inputs.to_list()
    expected_native_inputs = [
        file
        for file in pipeline_inputs
        if file != analyzed and file.basename not in ["debundle", "debundle.runfiles"]
    ]
    asserts.equals(
        env,
        sorted([file.path for file in expected_native_inputs]),
        sorted([file.path for file in native_inputs]),
    )
    asserts.false(env, analyzed in native_inputs)
    asserts.false(env, any([file.basename == "debundle" for file in native_inputs]))
    if ctx.attr.readable:
        export = actions["DebundleExportGraphs"]
        asserts.true(env, "--graphs --reports-only" in export.argv[-1])
        asserts.false(env, "--effect-conflicts" in export.argv[-1])
        asserts.false(env, "--full-report" in export.argv[-1])
        analyzed = actions["DebundleAnalyze"].outputs.to_list()[0]
        asserts.true(env, analyzed in actions["DebundlePipeline"].inputs.to_list())
        asserts.true(env, analyzed in export.inputs.to_list())
        asserts.equals(env, [target.label.name + ".dependency_graphs"], [file.basename for file in export.outputs.to_list()])
        asserts.equals(
            env,
            [target.label.name + ".dependency_graphs"],
            [file.basename for file in groups.dependency_graphs.to_list()],
        )
    return analysistest.end(env)

def _analysis_threads_action_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = {action.mnemonic: action for action in analysistest.target_actions(env)}
    asserts.equals(
        env,
        ["DebundleAnalyze", "DebundleExportGraphs", "DebundlePipeline", "DebundleProjectAnalysis"],
        sorted(actions),
    )
    asserts.equals(env, "2", actions["DebundleAnalyze"].env["RAYON_NUM_THREADS"])
    for mnemonic, action in actions.items():
        asserts.true(env, "BAZEL_BINDIR" in action.env)
        if mnemonic != "DebundleAnalyze":
            asserts.false(env, "RAYON_NUM_THREADS" in action.env)
    return analysistest.end(env)

analysis_threads_action_test = analysistest.make(_analysis_threads_action_test_impl)

pipeline_report_flags_test = analysistest.make(
    _pipeline_report_flags_test_impl,
    attrs = {
        "baseline": attr.label(aspects = [_baseline_actions], mandatory = True),
        "expected_flags": attr.string(),
        "readable": attr.bool(),
    },
)

def _analysis_identity_inputs_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [action for action in analysistest.target_actions(env) if action.mnemonic == "DebundleAnalysisIdentity"]
    asserts.equals(env, 1, len(actions))
    files = actions[0].inputs.to_list()
    for crate in ["staged_analysis", "prepare_chunks", "vendor", "analysis_codec", "analysis"]:
        asserts.true(env, any([file.basename.startswith("lib" + crate + "-") and file.extension == "rlib" for file in files]))
    for crate in ["pipeline", "lowering", "debundle_cli"]:
        asserts.false(env, any([file.basename.startswith("lib" + crate + "-") for file in files]))
    return analysistest.end(env)

analysis_identity_inputs_test = analysistest.make(_analysis_identity_inputs_test_impl)

_FamilyMemberInfo = provider(doc = "A family member's actions by mnemonic.", fields = ["actions"])

def _family_member_impl(target, _ctx):
    return [_FamilyMemberInfo(actions = {action.mnemonic: action for action in target.actions})]

_family_member = aspect(implementation = _family_member_impl)

def _basenames(files):
    return sorted([file.basename for file in files])

def _product_family_test_impl(ctx):
    env = analysistest.begin(ctx)
    target = analysistest.target_under_test(env)
    name = target.label.name
    actions = {action.mnemonic: action for action in analysistest.target_actions(env)}
    asserts.equals(
        env,
        ["DebundleAnalyze", "DebundleExportGraphs", "DebundlePipeline", "DebundleProjectAnalysis"],
        sorted(actions),
    )
    analyzed = actions["DebundleAnalyze"].outputs.to_list()[0]
    groups = target[OutputGroupInfo]
    asserts.equals(env, _basenames(groups.analysis.to_list()), _basenames(ctx.files.analysis))
    asserts.equals(env, [name + ".dependency_graphs"], _basenames(ctx.files.graphs))

    # A nested residual baseline's YAML is not an input of the outer spec.
    for mnemonic in ["DebundleProjectAnalysis", "DebundlePipeline"]:
        asserts.false(env, any(["/residual_baseline/" in file.path for file in actions[mnemonic].inputs.to_list()]))

    # Variants of the same spec register only emission and read the shared analysis.
    for variant in [ctx.attr.readable, ctx.attr.passing]:
        if variant:
            variant_actions = variant[_FamilyMemberInfo].actions
            asserts.equals(env, ["DebundlePipeline"], sorted(variant_actions))
            asserts.true(env, analyzed in variant_actions["DebundlePipeline"].inputs.to_list())
    if ctx.attr.readable:
        asserts.true(env, "--emit-on-rejection" in ctx.attr.readable[_FamilyMemberInfo].actions["DebundlePipeline"].argv[-1])
    if ctx.attr.passing:
        asserts.true(env, "--ownership-subset" in ctx.attr.passing[_FamilyMemberInfo].actions["DebundlePipeline"].argv[-1])

    baseline_actions = ctx.attr.residual_baseline[_FamilyMemberInfo].actions
    baseline_command = baseline_actions["DebundlePipeline"].argv[-1]
    for path in ["spec_config.yaml", "modules", "sources/vendor/vendor_marks.yaml"]:
        asserts.true(env, "product/spec/residual_baseline/" + path + "\"" in baseline_command)
    if ctx.attr.residual_baseline_reuses_analysis:
        asserts.equals(env, ["DebundlePipeline"], sorted(baseline_actions))
        asserts.true(env, analyzed in baseline_actions["DebundlePipeline"].inputs.to_list())
        baseline_analysis = groups.analysis.to_list()
    else:
        asserts.equals(env, ["DebundleAnalyze", "DebundlePipeline", "DebundleProjectAnalysis"], sorted(baseline_actions))
        asserts.equals(env, "2", baseline_actions["DebundleAnalyze"].env["RAYON_NUM_THREADS"])
        baseline_analysis = baseline_actions["DebundleAnalyze"].outputs.to_list() + baseline_actions["DebundleProjectAnalysis"].outputs.to_list()
    asserts.equals(env, _basenames(baseline_analysis), _basenames(ctx.files.residual_baseline_analysis))
    return analysistest.end(env)

product_family_test = analysistest.make(
    _product_family_test_impl,
    attrs = {
        "analysis": attr.label(allow_files = True, mandatory = True),
        "graphs": attr.label(allow_files = True, mandatory = True),
        "passing": attr.label(aspects = [_family_member]),
        "readable": attr.label(aspects = [_family_member]),
        "residual_baseline": attr.label(aspects = [_family_member], mandatory = True),
        "residual_baseline_analysis": attr.label(allow_files = True, mandatory = True),
        "residual_baseline_reuses_analysis": attr.bool(),
    },
)
