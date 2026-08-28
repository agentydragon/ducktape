"""The Codex `thread/start` params the runner composes from the console-selected launch."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from haku.runner.codex.harness import CodexAppServerError, _thread_params
from haku.runner.codex.options import CodexAppServerSession, build_codex_launch

_QUALIFIED_MODEL = "chatgpt/oai-responses/example-model"


def test_thread_start_carries_the_selected_model_effort_and_instructions() -> None:
    launch = build_codex_launch(
        CodexAppServerSession(
            cwd=Path("/workspace"),
            model=_QUALIFIED_MODEL,
            reasoning_effort="low",
            developer_instructions="you are Haku",
        )
    )

    assert _thread_params(launch) == {
        "cwd": "/workspace",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "ephemeral": True,
        "model": _QUALIFIED_MODEL,
        "config": {"model_reasoning_effort": "low"},
        "developerInstructions": "you are Haku",
    }


def test_thread_start_omits_optional_effort_and_instructions_when_unset() -> None:
    launch = build_codex_launch(CodexAppServerSession(cwd=Path("/workspace"), model=_QUALIFIED_MODEL))

    params = _thread_params(launch)

    assert params["model"] == _QUALIFIED_MODEL
    assert "config" not in params
    assert "developerInstructions" not in params


def test_thread_start_rejects_a_provider_less_model() -> None:
    launch = build_codex_launch(CodexAppServerSession(cwd=Path("/workspace"), model="bare-model-name"))

    with pytest.raises(CodexAppServerError, match="provider-qualified"):
        _thread_params(launch)


def test_thread_start_rejects_a_missing_model() -> None:
    launch = build_codex_launch(CodexAppServerSession(cwd=Path("/workspace")))

    with pytest.raises(CodexAppServerError, match="provider-qualified"):
        _thread_params(launch)


if __name__ == "__main__":
    pytest_bazel.main()
