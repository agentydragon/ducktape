"""Tests for Claude Code tool input models."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.claude.claude_api.tool_input_models import (
    BashInput,
    EditInput,
    GrepInput,
    GrepOutputMode,
    ReadInput,
    WriteInput,
)


class TestBashInput:
    def test_minimal(self) -> None:
        m = BashInput.model_validate({"command": "ls"})
        assert m.command == "ls"
        assert m.timeout is None
        assert m.run_in_background is None

    def test_all_fields(self) -> None:
        m = BashInput.model_validate(
            {
                "command": "echo hi",
                "description": "Print greeting",
                "timeout": 5000,
                "run_in_background": True,
                "dangerouslyDisableSandbox": True,
            }
        )
        assert m.command == "echo hi"
        assert m.dangerously_disable_sandbox is True
        assert m.run_in_background is True

    def test_missing_command_raises(self) -> None:
        with pytest.raises(ValidationError):
            BashInput.model_validate({"description": "oops"})

    def test_extra_fields_allowed(self) -> None:
        m = BashInput.model_validate({"command": "ls", "futureField": 42})
        assert m.command == "ls"


class TestEditInput:
    def test_parse(self) -> None:
        m = EditInput.model_validate({"file_path": "/tmp/foo.py", "old_string": "a", "new_string": "b"})
        assert m.file_path == "/tmp/foo.py"
        assert m.replace_all is False

    def test_replace_all(self) -> None:
        m = EditInput.model_validate(
            {"file_path": "/tmp/foo.py", "old_string": "a", "new_string": "b", "replace_all": True}
        )
        assert m.replace_all is True


class TestWriteInput:
    def test_parse(self) -> None:
        m = WriteInput.model_validate({"file_path": "/tmp/f.py", "content": "hello"})
        assert m.file_path == "/tmp/f.py"
        assert m.content == "hello"


class TestReadInput:
    def test_with_pages(self) -> None:
        m = ReadInput.model_validate({"file_path": "/tmp/f.pdf", "pages": "1-5"})
        assert m.pages == "1-5"


class TestGrepInput:
    def test_dash_aliases(self) -> None:
        m = GrepInput.model_validate({"pattern": "foo", "-B": 3, "-A": 5, "-C": 2, "-n": True, "-i": True})
        assert m.before_context == 3
        assert m.after_context == 5
        assert m.context == 2
        assert m.line_numbers is True
        assert m.case_insensitive is True

    def test_output_mode_enum(self) -> None:
        m = GrepInput.model_validate({"pattern": "x", "output_mode": "content"})
        assert m.output_mode is GrepOutputMode.CONTENT

    def test_extra_fields_allowed(self) -> None:
        m = GrepInput.model_validate({"pattern": "x", "newUpstreamFlag": True})
        assert m.pattern == "x"


if __name__ == "__main__":
    pytest_bazel.main()
