"""Analysis tests for the Bazel-driven debundle pipeline."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")
load(":pipeline.bzl", "debundle_pipeline")

def _multi_chunk_diagnostics_use_per_solve_directories_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [
        action
        for action in analysistest.target_actions(env)
        if action.mnemonic == "DebundlePipeline"
    ]
    asserts.equals(env, 1, len(actions))
    if actions:
        command = " ".join(actions[0].argv)
        asserts.true(
            env,
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO_DIR=" in command,
            "the parallel pipeline must give each selector solve a distinct request path",
        )
        asserts.true(
            env,
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR=" in command,
            "the parallel pipeline must give each selector solve a distinct summary path",
        )
        asserts.false(
            env,
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO=" in command,
            "a fixed request path races across parallel chunk solves",
        )
        asserts.false(
            env,
            "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON=" in command,
            "a fixed summary path races across parallel chunk solves",
        )
    return analysistest.end(env)

multi_chunk_diagnostics_use_per_solve_directories_test = analysistest.make(
    _multi_chunk_diagnostics_use_per_solve_directories_impl,
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
    multi_chunk_diagnostics_use_per_solve_directories_test(
        name = name,
        target_under_test = ":" + subject,
    )
