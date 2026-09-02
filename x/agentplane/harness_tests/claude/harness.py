"""The pinned Claude Code binary wired to a scripted upstream."""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import scenarios
from x.agentplane.native.process import NativeProcess

# A routed name in the shape a LiteLLM deployment gives Claude Code; the family suffix lets it
# resolve the model's context window. The scripted upstream never dispatches on it.
MODEL = "agentplane-test/claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class ClaudeHarness:
    workspace: Path
    logs: Path
    config: Path
    binary: str
    base_environment: dict[str, str]

    def start(self, upstream: ScriptedUpstream, *, resume_id: str | None = None) -> NativeProcess:
        command = [_dynamic_loader(), *scenarios.command(self.binary, model=MODEL, resume_id=resume_id)]
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=upstream.origin, token="test-key", config_dir=str(self.config)),
        }
        process = NativeProcess(self.logs, command, cwd=self.workspace, environment=environment)
        process.frame_handler = _allow_permission
        return process


def _dynamic_loader() -> str:
    # TODO: run Claude in RBE without this Nix ELF-loader workaround.
    data = Path(sys.executable).resolve().read_bytes()
    if data[:4] != b"\x7fELF":
        raise RuntimeError("Bazel Python is not an ELF executable")
    program_offset = struct.unpack_from("<Q", data, 32)[0]
    program_size = struct.unpack_from("<H", data, 54)[0]
    program_count = struct.unpack_from("<H", data, 56)[0]
    for index in range(program_count):
        offset = program_offset + index * program_size
        if struct.unpack_from("<I", data, offset)[0] != 3:
            continue
        interpreter_offset = struct.unpack_from("<Q", data, offset + 8)[0]
        interpreter_size = struct.unpack_from("<Q", data, offset + 32)[0]
        return data[interpreter_offset : interpreter_offset + interpreter_size].rstrip(b"\0").decode()
    raise RuntimeError("Bazel Python has no ELF interpreter")


def _allow_permission(frame: dict[str, Any]) -> dict[str, Any] | None:
    request = frame.get("request")
    if not isinstance(request, dict) or request.get("subtype") != "can_use_tool":
        return None
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": frame["request_id"],
            "response": {"behavior": "allow", "updatedInput": request.get("input")},
        },
    }
