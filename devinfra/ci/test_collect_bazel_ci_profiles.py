import pytest_bazel

from devinfra.ci import collect_bazel_ci_profiles


def test_parse_log() -> None:
    parsed = collect_bazel_ci_profiles.parse_log(
        """
CI_VM_PROBE_SUMMARY phase=before-test out=/tmp/probes.jsonl previous_run_log=yes stale_current_log=no bazel_servers=1 boot_id=abc uptime=123.4
CI_VM_PROBE_SERVER pid=556 age=945.0s start=2026-06-10T23:04:45.250000+00:00 sha256=abc123
\x1b[32mINFO: \x1b[mInvocation ID: 80d84017-6804-4fe6-9fdd-4562ff8ea039
\x1b[32mAnalyzing: \x1b[m3532 targets (3 packages loaded, 0 targets configured)
\x1b[32mINFO: \x1b[mElapsed time: 56.063s, Critical Path: 49.16s
\x1b[32mINFO: \x1b[m57 processes: 4774 action cache hit, 31 remote cache hit, 21 internal, 5 remote.
\x1b[32mINFO: \x1b[mInvocation ID: b6a769d4-1f64-4770-a18b-d1eb85d72a88
\x1b[32mAnalyzing: \x1b[m3532 targets (0 packages loaded, 0 targets configured)
\x1b[32mINFO: \x1b[mElapsed time: 7.513s, Critical Path: 0.06s
\x1b[32mINFO: \x1b[m1 process: 157 action cache hit, 1 internal.
CI_VM_PROBE_ARCHIVE path=/tmp/probe.tgz size=15132 sha256=hash
CI_VM_PROBE_CAS digest=/compressed-blobs/zstd/hash/15132
CI_VM_PROBE_CAS digest=
"""
    )

    assert parsed["probe_summaries"] == [
        {
            "phase": "before-test",
            "out": "/tmp/probes.jsonl",
            "previous_run_log": "yes",
            "stale_current_log": "no",
            "bazel_servers": "1",
            "boot_id": "abc",
            "uptime": "123.4",
        }
    ]
    assert parsed["probe_servers"][0]["pid"] == "556"
    assert parsed["probe_cas"] == [{"digest": "/compressed-blobs/zstd/hash/15132"}, {"missing_digest": "empty"}]

    test, build = parsed["invocations"]
    assert test["role"] == "test"
    assert test["id"] == "80d84017-6804-4fe6-9fdd-4562ff8ea039"
    assert test["elapsed_s"] == 56.063
    assert test["critical_path_s"] == 49.16
    assert test["process_count"] == 57
    assert test["max_packages_loaded"] == 3
    assert test["max_targets_configured"] == 0

    assert build["role"] == "build"
    assert build["elapsed_s"] == 7.513
    assert build["process_count"] == 1


def test_parse_log_with_github_rendered_ansi() -> None:
    parsed = collect_bazel_ci_profiles.parse_log(
        """
^[[32mINFO: ^[[mInvocation ID: 80d84017-6804-4fe6-9fdd-4562ff8ea039
^[[32mAnalyzing: ^[[m3532 targets (0 packages loaded, 0 targets configured
^[[32mINFO: ^[[mElapsed time: 56.063s, Critical Path: 49.16s
"""
    )

    test = parsed["invocations"][0]
    assert test["max_packages_loaded"] == 0
    assert test["max_targets_configured"] == 0


def test_bes_summary_extracts_analysis_metrics() -> None:
    summary = collect_bazel_ci_profiles.bes_summary(
        [
            {"started": {"uuid": "80d84017-6804-4fe6-9fdd-4562ff8ea039", "command": "test", "serverPid": "556"}},
            {
                "buildMetrics": {
                    "packageMetrics": {"packagesLoaded": "3"},
                    "targetMetrics": {"targetsConfigured": "1442", "targetsConfiguredNotIncludingAspects": "1310"},
                    "timingMetrics": {"analysisPhaseTimeInMs": "5373", "executionPhaseTimeInMs": "51689"},
                    "actionSummary": {
                        "actionsExecuted": "57",
                        "actionCacheStatistics": {
                            "hits": "4774",
                            "misses": "64",
                            "missDetails": [{"reason": "DIFFERENT_FILES", "count": "31"}],
                        },
                        "runnerCount": [{"name": "remote", "count": "5"}],
                    },
                }
            },
        ]
    )

    assert summary["started"]["server_pid"] == 556
    metrics = summary["build_metrics"]
    assert metrics["packages_loaded"] == 3
    assert metrics["targets_configured"] == 1442
    assert metrics["targets_configured_not_including_aspects"] == 1310
    assert metrics["analysis_phase_s"] == 5.373
    assert metrics["execution_phase_s"] == 51.689
    assert metrics["actions_executed"] == 57
    assert metrics["ac_hits"] == 4774
    assert metrics["ac_misses"] == 64
    assert metrics["ac_miss_details"] == {"DIFFERENT_FILES": 31}
    assert metrics["runner_counts"] == {"remote": 5}


if __name__ == "__main__":
    pytest_bazel.main()
