"""Action-level controls for the public debundle pipeline's report flags."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")

_BaselineActions = provider(fields = ["actions"])

def _package_root_fixture_impl(ctx):
    package_root = ctx.actions.declare_directory(ctx.label.name)
    ctx.actions.run_shell(
        outputs = [package_root],
        arguments = [package_root.path],
        command = "mkdir -p \"$1\" && printf '%s\\n' '{\"name\":\"fixture-package\"}' > \"$1/package.json\"",
    )
    return [DefaultInfo(files = depset([package_root]))]

package_root_fixture = rule(implementation = _package_root_fixture_impl)

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
    expected_actions = ["DebundleApply"]
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
        if mnemonic == "DebundleApply" and ctx.attr.expected_flags:
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
    command = actions["DebundleApply"].argv[-1]
    for mnemonic in [name for name in expected_actions if name in ["DebundleApply", "DebundleExportGraphs"]]:
        files = actions[mnemonic].inputs.to_list()
        asserts.true(env, any([file.basename == "debundle" for file in files]))
        asserts.false(env, any([file.basename == "debundle_analyze" for file in files]))
        asserts.false(env, any([file.basename == "debundle_project_analysis" for file in files]))
    asserts.false(env, "--effect-conflicts" in baseline["DebundleApply"].argv[-1])
    asserts.false(env, "--full-report" in baseline["DebundleApply"].argv[-1])
    asserts.equals(env, ctx.attr.readable, '[ "$status" -eq 3 ] && [ -f ' in command)
    if ctx.attr.readable:
        asserts.true(env, target.label.name + ".out/GATE_REJECTED" in command)

    asserts.false(env, "--graphs" in command)
    asserts.equals(env, [target.label.name + ".out"], [file.basename for file in actions["DebundleApply"].outputs.to_list()])

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
    analyzed = [file for file in groups.analysis.to_list() if file.extension == "analysis"][0]
    native_inputs = groups.native_inputs.to_list()
    projected = groups.apply_projection.to_list()[0]
    apply_source_inputs = [
        file
        for file in actions["DebundleApply"].inputs.to_list()
        if file not in [analyzed, projected] and file.basename not in ["debundle", "debundle.runfiles"]
    ]
    pipeline_inputs = actions["DebundleApply"].inputs.to_list()
    asserts.false(env, any([file.extension in ["yaml", "yml"] for file in pipeline_inputs]))
    asserts.true(env, projected in pipeline_inputs)
    asserts.true(env, any([file.extension in ["yaml", "yml"] for file in native_inputs]))
    for file in apply_source_inputs:
        asserts.true(env, file in native_inputs)
    asserts.false(env, analyzed in native_inputs)
    asserts.false(env, any([file.basename == "debundle" for file in native_inputs]))
    if ctx.attr.readable:
        export = actions["DebundleExportGraphs"]
        asserts.true(env, "--graphs --reports-only" in export.argv[-1])
        asserts.false(env, "--effect-conflicts" in export.argv[-1])
        asserts.false(env, "--full-report" in export.argv[-1])
        analyzed = [file for file in groups.analysis.to_list() if file.extension == "analysis"][0]
        asserts.true(env, analyzed in actions["DebundleApply"].inputs.to_list())
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
    asserts.equals(env, ["DebundleAnalyze", "DebundleProjectAnalysis"], sorted(actions))
    asserts.equals(env, "2", actions["DebundleAnalyze"].env["RAYON_NUM_THREADS"])
    asserts.false(env, "RAYON_NUM_THREADS" in actions["DebundleProjectAnalysis"].env)
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

def _apply_projection_inputs_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = {action.mnemonic: action for action in analysistest.target_actions(env)}
    projection_action = actions["DebundleProjectApply"]
    apply_action = actions["DebundleApply"]
    projection_inputs = [file.path for file in projection_action.inputs.to_list()]
    apply_inputs = [file.path for file in apply_action.inputs.to_list()]

    for suffix in ctx.attr.spec_inputs:
        asserts.true(
            env,
            any([path.endswith(suffix) for path in projection_inputs]),
            "DebundleProjectApply must read spec input {}".format(suffix),
        )
    for suffix in ctx.attr.apply_only_inputs:
        asserts.false(
            env,
            any([path.endswith(suffix) for path in projection_inputs]),
            "DebundleProjectApply must not depend on source/package input {}".format(suffix),
        )
        asserts.true(
            env,
            any([path.endswith(suffix) for path in apply_inputs]),
            "DebundleApply must retain source/package input {}".format(suffix),
        )

    asserts.false(env, "--package-root" in projection_action.argv[-1])
    asserts.true(env, "--package-root" in apply_action.argv[-1])
    projection = projection_action.outputs.to_list()[0]
    asserts.true(env, projection in apply_action.inputs.to_list())
    return analysistest.end(env)

apply_projection_inputs_test = analysistest.make(
    _apply_projection_inputs_test_impl,
    attrs = {
        "spec_inputs": attr.string_list(mandatory = True),
        "apply_only_inputs": attr.string_list(mandatory = True),
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

def _shared_analysis_test_impl(ctx):
    env = analysistest.begin(ctx)
    target = analysistest.target_under_test(env)
    actions = {action.mnemonic: action for action in analysistest.target_actions(env)}
    asserts.equals(env, ["DebundleAnalyze", "DebundleProjectAnalysis"], sorted(actions))
    analyzed = actions["DebundleAnalyze"].outputs.to_list()[0]
    projected_analysis = [file for file in actions["DebundleProjectAnalysis"].outputs.to_list() if file.extension == "analysis_projection"][0]
    projected_apply = [file for file in actions["DebundleProjectAnalysis"].outputs.to_list() if file.extension == "apply_projection"][0]
    analysis_inputs = actions["DebundleAnalyze"].inputs.to_list()
    asserts.true(env, any([file.basename == "debundle_analyze" for file in analysis_inputs]))
    asserts.false(env, any([file.extension in ["yaml", "yml"] for file in analysis_inputs]))
    asserts.false(env, any([file.basename in ["debundle", "debundle_project_analysis"] for file in analysis_inputs]))
    project_inputs = actions["DebundleProjectAnalysis"].inputs.to_list()
    asserts.true(env, any([file.basename == "debundle_project_analysis" for file in project_inputs]))
    asserts.false(env, any([file.basename in ["debundle", "debundle_analyze"] for file in project_inputs]))
    asserts.equals(env, [projected_analysis, projected_apply], actions["DebundleProjectAnalysis"].outputs.to_list())
    asserts.true(env, projected_analysis in analysis_inputs)
    asserts.false(env, projected_apply in analysis_inputs)
    asserts.equals(env, [projected_apply], target[OutputGroupInfo].apply_projection.to_list())
    for variant in ctx.attr.variants:
        applied = {action.mnemonic: action for action in variant[_BaselineActions].actions}
        asserts.equals(env, ["DebundleApply"], sorted(applied))
        asserts.true(env, analyzed in applied["DebundleApply"].inputs.to_list())
        asserts.true(env, projected_apply in applied["DebundleApply"].inputs.to_list())
    return analysistest.end(env)

shared_analysis_test = analysistest.make(
    _shared_analysis_test_impl,
    attrs = {"variants": attr.label_list(aspects = [_baseline_actions], mandatory = True)},
)
