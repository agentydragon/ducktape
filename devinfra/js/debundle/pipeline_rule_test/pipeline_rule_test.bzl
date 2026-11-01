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
    asserts.false(env, "--effect-conflicts" in baseline["DebundlePipeline"].argv[-1])
    asserts.false(env, "--full-report" in baseline["DebundlePipeline"].argv[-1])
    asserts.equals(env, ctx.attr.readable, '[ "$status" -eq 3 ] || exit "$status"' in command)

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

pipeline_report_flags_test = analysistest.make(
    _pipeline_report_flags_test_impl,
    attrs = {
        "baseline": attr.label(aspects = [_baseline_actions], mandatory = True),
        "expected_flags": attr.string(),
        "readable": attr.bool(),
    },
)
