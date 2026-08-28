"""Test file truncation logic to prevent OpenAI API limit errors."""

import json

import pytest
import pytest_bazel
import tiktoken

from x.inop.engine.models import FileInfo
from x.inop.prompting.truncation_utils import TruncationManager


@pytest.mark.usefixtures("tiktoken_cache")
class TestFileTruncation:
    """Test centralized file truncation logic."""

    def test_files_under_limit_unchanged(self, test_config):
        """Small files should pass through unchanged."""
        files = [FileInfo(path="small.py", content="print('hello')"), FileInfo(path="tiny.txt", content="small file")]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        assert result == files
        assert len(result) == 2

    def test_token_limit_assertion(self, test_config):
        """Result should never exceed the token limit."""
        files = []
        for i in range(20):
            content = "def function_" + str(i) + "():\n    " + "print('test')\n" * 1000
            files.append(FileInfo(path=f"test_{i}.py", content=content))
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        encoding = tiktoken.encoding_for_model(test_config.grader.model)
        files_json = json.dumps([f.model_dump() for f in result], indent=2)
        final_tokens = len(encoding.encode(files_json))
        max_files_tokens = 150_000
        assert final_tokens <= max_files_tokens, f"Result exceeds limit: {final_tokens} > {max_files_tokens}"

    def test_largest_files_truncated_first(self, test_config):
        """Largest files should be truncated before smaller ones."""
        files = [
            FileInfo(path="small.py", content="print('small')"),
            FileInfo(path="medium.py", content="x" * 1000),
            FileInfo(path="large.py", content="y" * 10000),
        ]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        small_file = next(f for f in result if f.path == "small.py")
        assert small_file.content == "print('small')"
        for file_info in result:
            if "TRUNCATED" in file_info.content:
                assert file_info.path in ["large.py", "medium.py"]

    def test_empty_files_list(self, test_config):
        """Empty input should return empty output."""
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens([], 150_000)
        assert result == []


if __name__ == "__main__":
    pytest_bazel.main()
