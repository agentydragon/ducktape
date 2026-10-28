"""Instrumented native-module startup with explicitly synthetic boundary inputs."""

import json
import subprocess
from pathlib import Path

import pytest_bazel

from agentplane.native.claude.x.remote_tools import probe
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_native_serving(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("ROOT_REMOTE_INSTRUCTION_7b32c8\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "CLAUDE.md").write_text("NESTED_REMOTE_INSTRUCTION_5d61a4\n")
    target = nested / "editable.txt"
    target.write_text("before\n")
    script = get_required_path("_main/agentplane/native/claude/x/remote_tools/native_probe.mjs")
    completed = probe.run(script, workspace=tmp_path, target=target, artifacts=undeclared_outputs_dir())
    (undeclared_outputs_dir() / "native-source-stdout.jsonl").write_text(completed.stdout)
    (undeclared_outputs_dir() / "native-source-stderr.log").write_text(completed.stderr)
    assert completed.returncode == 0, completed.stderr[-2500:] + completed.stdout[-2500:]
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    events = {
        name: [record["value"] for record in records if record["name"] == name] for name in {r["name"] for r in records}
    }

    default = events["default-gates"][0]
    assert default["enabled"] is False
    assert default["egress"] == "egress"
    assert default["account"]["status"] == "missing"
    simulated = events["synthetic-cache-gates"][0]
    assert simulated["enabled"] is True
    assert simulated["egress"] is None
    assert simulated["account"]["status"] == "resolved"
    assert simulated["account"]["source"] == "stored"
    for flag in ("wood", "willow"):
        assert default[flag] == {"value": False, "source": "disabled"}
        assert simulated[flag] == {"value": True, "source": "disk"}
    assert events["started"] == [True]
    assert {"get_device_info", "Bash", "Read", "Edit", "Write"} <= set(events["roster"][0])
    connection = next(message for message in events["bridge-received"] if message.get("type") == "connect")
    assert connection["oauth_token"] == "synthetic-loopback-token"
    assert connection["session_id"] == "00000000-0000-4000-8000-000000000001"
    assert connection["rtb_protocol_version"] == 2
    assert connection["hb_capable"] is True
    assert connection["cancel_capable"] is True
    refused = events["raw-read"][0]["result"]
    assert refused["isError"] is True
    assert refused["structuredContent"]["anthropic/remoteToolExecution"]["code"] == "invalid_request"

    for name in ("edit-unread", "edit-after-read"):
        ask = events[name][0]["result"]["structuredContent"]["anthropic/remoteToolExecution"]
        assert ask["outcome"] == "needs_approval"
        assert ask["ask_id"]
        approved = events[f"{name}-approved"][0]["result"]["structuredContent"]["anthropic/remoteToolExecution"]
        assert approved["outcome"] == "completed"
        assert approved["is_error"] is False
        assert approved["disposition"] == "approved_by_session"
    assert events["file-before-approval"] == [
        {"callId": "edit-unread", "content": "before\n"},
        {"callId": "edit-after-read", "content": "unread\n"},
    ]
    first_read, replay = [item["result"] for item in events["read-native"]]
    read_execution = first_read["structuredContent"]["anthropic/remoteToolExecution"]
    assert read_execution["outcome"] == "completed"
    assert read_execution["is_error"] is False
    assert "unread" in first_read["content"][0]["text"]
    # This checks only the serving payload, not independent discovery by the cloud caller.
    assert "ROOT_REMOTE_INSTRUCTION_7b32c8" not in json.dumps(first_read)
    assert "NESTED_REMOTE_INSTRUCTION_5d61a4" not in json.dumps(first_read)
    replay_execution = replay["structuredContent"]["anthropic/remoteToolExecution"]
    assert replay_execution["replayed"] is True
    assert replay["content"] == first_read["content"]
    states = [item["observation"] for item in events["server-read-state"]]
    assert states[0]["content"] == "unread\n"
    assert states[0]["contentNotInModelContext"] is True
    assert states[1]["content"] == "unread\n"
    assert states[1]["offset"] == 1
    assert "contentNotInModelContext" not in states[1]
    assert states[2]["content"] == "after\n"
    assert states[3] == states[2]  # A replay returns cached content without rereading the file.
    assert target.read_text() == "after\n"
    mismatch = events["mismatch-read"][0]["result"]
    assert mismatch["isError"] is True
    # This MCP-layer denial has no structured error code; assert its specific detail and native trace.
    assert "different Claude Code cloud session" in mismatch["content"][0]["text"]
    assert (
        '[deviceBridge] refused Read: call asserted for session "00000000-0000-4000-8000-000000000099", '
        "this device serves 00000000-0000-4000-8000-000000000001"
    ) in completed.stderr


def test_unmodified_cloud_startup_requires_login(tmp_path: Path, base_environment: dict[str, str]) -> None:
    completed = subprocess.run(
        [
            str(get_required_path("claude_remote_tools_cli_linux_x64/claude")),
            "--cloud",
            "session_synthetic_remote_tools_probe",
        ],
        cwd=tmp_path,
        env={
            **base_environment,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        },
        input="synthetic diagnostic prompt\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    (undeclared_outputs_dir() / "unmodified-cloud-startup.json").write_text(
        json.dumps(
            {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, indent=2
        )
    )
    assert completed.returncode != 0
    assert "Cloud sessions need a claude.ai sign-in" in completed.stdout + completed.stderr


if __name__ == "__main__":
    pytest_bazel.main()
