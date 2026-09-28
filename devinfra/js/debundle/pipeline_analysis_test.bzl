"""Analysis tests for the Bazel-driven debundle pipeline."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")
load(":pipeline.bzl", "debundle_pipeline")

def _pipeline_action_reaches_the_solver_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [
        action
        for action in analysistest.target_actions(env)
        if action.mnemonic == "DebundlePipeline"
    ]
    asserts.equals(env, 1, len(actions))
    if actions:
        # The action has no runfiles tree, so the debundler finds the CP-SAT
        # sidecar only through this variable.
        asserts.true(
            env,
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER=" in " ".join(actions[0].argv),
            "the pipeline action must name the CP-SAT sidecar",
        )
    return analysistest.end(env)

pipeline_action_reaches_the_solver_test = analysistest.make(
    _pipeline_action_reaches_the_solver_impl,
)

def pipeline_analysis_test_suite(name):
    subject = name + "_subject"
    source_root = name + "_source_root"
    native.filegroup(
        name = source_root,
        srcs = ["pipeline_analysis_test_spec.yaml"],
    )
    debundle_pipeline(
        name = subject,
        spec = "pipeline_analysis_test_spec.yaml",
        tags = ["manual"],
        tree_source_root = ":" + source_root,
    )
    pipeline_action_reaches_the_solver_test(
        name = name,
        target_under_test = ":" + subject,
    )
