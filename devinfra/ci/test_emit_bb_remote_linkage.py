from pathlib import Path

import pytest_bazel

from devinfra.ci import emit_bb_remote_linkage


def test_build_record() -> None:
    record = emit_bb_remote_linkage.build_record(
        log_text="""
Streaming remote runner logs to: https://app.buildbuddy.io/invocation/cab7b556-8bc9-46fc-8f9d-54b880ef4153
\x1b[32mINFO: \x1b[mInvocation ID: 34f127a3-16a7-43f7-a590-937326e19fe4
CI_VM_PROBE_CAS digest=/compressed-blobs/zstd/abc/123
\x1b[32mINFO: \x1b[mInvocation ID: dab41d17-9528-48f9-8e93-d94cfab847ba
CI_VM_PROBE_CAS digest=
""",
        log_path=Path("/tmp/bb-remote.log"),
        roles=["test", "build"],
        env={
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "agentydragon/ducktape",
            "GITHUB_WORKFLOW": "Bazel CI",
            "GITHUB_RUN_ID": "27313474860",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_JOB": "bazel-ci",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/2021/merge",
            "GITHUB_SHA": "e9307e501a1bfff111b6432cf970fc2fb2a41d1b",
            "GITHUB_HEAD_REF": "optimize-debundle",
            "GITHUB_BASE_REF": "devel",
        },
        bb_remote_exit_code=0,
    )

    assert record["schema"] == "ducktape.bb_remote_linkage.v1"
    assert record["github"]["run_id"] == "27313474860"
    assert record["buildbuddy"]["runner_invocation_id"] == "cab7b556-8bc9-46fc-8f9d-54b880ef4153"
    assert record["buildbuddy"]["bazel_invocations"] == [
        {
            "index": 0,
            "role": "test",
            "invocation_id": "34f127a3-16a7-43f7-a590-937326e19fe4",
            "build_tool_log_names": ["command.profile.gz", "critical path", "elapsed time", "process stats"],
        },
        {
            "index": 1,
            "role": "build",
            "invocation_id": "dab41d17-9528-48f9-8e93-d94cfab847ba",
            "build_tool_log_names": ["command.profile.gz", "critical path", "elapsed time", "process stats"],
        },
    ]
    assert record["buildbuddy"]["probe_cas"] == [
        {"digest": "/compressed-blobs/zstd/abc/123"},
        {"missing_digest": "empty"},
    ]
    assert record["warnings"] == []


def test_missing_ids_are_warnings() -> None:
    record = emit_bb_remote_linkage.build_record(
        log_text="no useful lines", log_path=Path("/tmp/bb-remote.log"), roles=[], env={}, bb_remote_exit_code=1
    )

    assert record["buildbuddy"].get("runner_invocation_id") is None
    assert record["buildbuddy"]["bazel_invocations"] == []
    assert record["warnings"] == ["runner invocation id not found", "child Bazel invocation ids not found"]


if __name__ == "__main__":
    pytest_bazel.main()
