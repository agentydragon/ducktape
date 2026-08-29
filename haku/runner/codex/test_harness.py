"""The Codex `thread/start` params the runner composes from the console-selected launch."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from haku.runner.codex.harness import _thread_params
from haku.runner.codex.options import CodexAppServerSession, build_codex_launch

_MODEL = "example-model"


def test_thread_start_carries_the_selected_model_effort_and_instructions() -> None:
    launch = build_codex_launch(
        CodexAppServerSession(
            cwd=Path("/workspace"), model=_MODEL, reasoning_effort="low", developer_instructions="you are Haku"
        )
    )

    assert _thread_params(launch) == {
        "cwd": "/workspace",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "ephemeral": True,
        "model": _MODEL,
        "config": {"model_reasoning_effort": "low"},
        "developerInstructions": "you are Haku",
    }


def test_thread_start_omits_optional_effort_and_instructions_when_unset() -> None:
    launch = build_codex_launch(CodexAppServerSession(cwd=Path("/workspace"), model=_MODEL))

    params = _thread_params(launch)

    assert params["model"] == _MODEL
    assert "config" not in params
    assert "developerInstructions" not in params


if __name__ == "__main__":
    pytest_bazel.main()
