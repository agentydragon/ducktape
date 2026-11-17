"""Select benchmark targets and replace this process with their Bazel invocation."""

import base64
import os
import shlex
import subprocess
import sys


def main() -> None:
    suite = os.environ.get("DEBUNDLE_BENCH_SUITE", "excalidraw")
    scopes = {
        "excalidraw": "//devinfra/js/debundle/perf:excalidraw_pipeline_bench",
        "bench": "//devinfra/js/debundle/e2e/bench/...",
        "all": "//devinfra/js/debundle/...",
    }
    profile = os.environ.get("DEBUNDLE_BENCH_PROFILE", "none")
    if suite not in scopes or profile not in ("none", "cpu", "heap"):
        sys.exit("unsupported benchmark suite or profile setting")
    if suite == "bench" and profile != "none":
        sys.exit("profiling requires the Excalidraw workload")
    flags = shlex.split(
        base64.b64decode(os.environ.get("DEBUNDLE_BENCH_ADDITIONAL_TEST_FLAGS_B64", ""), validate=True).decode()
    )
    if profile != "none":
        flags.extend(
            [
                "--@rules_rust//rust/settings:extra_rustc_flag=-Cdebuginfo=1",
                "--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes",
                "--strip=never",
            ]
        )
    # Query output includes manual targets; wildcard test expansion excludes them.
    targets = subprocess.check_output(
        ["bazelisk", "query", f'kind(".*_test rule", attr("tags", "bench", {scopes[suite]}))'], text=True
    ).splitlines()
    if not targets:
        sys.exit("benchmark query returned no tests")
    os.execvp(
        "bazelisk",
        [
            "bazelisk",
            "test",
            "--config=rbe",
            "--config=ci",
            "--remote_default_exec_properties=container-image=docker://" + os.environ["RBE_CONTAINER_IMAGE"],
            *flags,
            "-c",
            "opt",
            "--test_tag_filters=bench",
            "--nocache_test_results",
            "--remote_build_event_upload=all",
            "--invocation_id=" + os.environ["DEBUNDLE_BENCH_INVOCATION_ID"],
            "--build_metadata=TAG_debundle-bench=",
            "--build_metadata=TAG_suite=" + suite,
            "--build_metadata=TAG_profile=" + profile,
            "--test_env=DEBUNDLE_BENCH_PROFILE=" + profile,
            "--run_under=prlimit --as=17179869184 --fsize=536870912 --",
            *targets,
        ],
    )


if __name__ == "__main__":
    main()
