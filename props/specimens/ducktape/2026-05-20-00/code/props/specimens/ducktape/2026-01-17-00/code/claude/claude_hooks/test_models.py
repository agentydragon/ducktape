"""Tests for hook models."""

from pathlib import Path
from uuid import uuid4

import pytest
from hamcrest import assert_that, has_properties
from pydantic import ValidationError

from claude_hooks.inputs import PostToolInput, PreToolInput, UserPromptSubmitInput
from claude_hooks.tool_models import BashInput, EditInput, WriteInput


def test_edit_input_valid():
    data = {"file_path": "/tmp/test.py", "old_string": "old code", "new_string": "new code", "replace_all": False}

    edit_input = EditInput.model_validate(data)
    assert edit_input == EditInput(
        file_path=Path("/tmp/test.py"), old_string="old code", new_string="new code", replace_all=False
    )


def test_edit_input_missing_required():
    data = {
        "file_path": "/tmp/test.py",
        "old_string": "old code",
        # missing new_string
    }

    with pytest.raises(ValidationError):
        EditInput.model_validate(data)


def test_write_input_valid():
    data = {"file_path": "/tmp/test.py", "content": "print('hello')"}

    write_input = WriteInput.model_validate(data)
    assert write_input == WriteInput(file_path=Path("/tmp/test.py"), content="print('hello')")


def test_bash_input_valid():
    data = {"command": "ls -la", "description": "List files", "timeout": 30}

    bash_input = BashInput.model_validate(data)
    assert bash_input == BashInput(command="ls -la", description="List files", timeout=30)


def test_bash_input_minimal():
    data = {"command": "pwd"}

    bash_input = BashInput.model_validate(data)
    assert bash_input == BashInput(command="pwd", description=None, timeout=None)


def test_pre_tool_use_input():
    session_id = uuid4()
    data = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/test.py", "old_string": "old", "new_string": "new"},
        "session_id": str(session_id),
        "transcript_path": "/tmp/transcript.json",
        "cwd": "/tmp",
    }

    input_obj = PreToolInput.model_validate(data)
    assert_that(input_obj, has_properties(tool_name="Edit", session_id=session_id, cwd=Path("/tmp")))


def test_post_tool_use_input():
    session_id = uuid4()
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/test.py", "content": "print('hello')"},
        "tool_response": {"success": True},
        "session_id": str(session_id),
        "transcript_path": "/tmp/transcript.json",
        "cwd": "/tmp",
    }

    input_obj = PostToolInput.model_validate(data)
    assert_that(input_obj, has_properties(tool_name="Write", tool_response={"success": True}))


def test_user_prompt_submit_input():
    session_id = uuid4()
    data = {
        "prompt": "Write a function",
        "session_id": str(session_id),
        "transcript_path": "/tmp/transcript.json",
        "cwd": "/tmp",
    }

    input_obj = UserPromptSubmitInput.model_validate(data)
    assert input_obj.prompt == "Write a function"
    assert input_obj.session_id == session_id
