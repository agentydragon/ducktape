"""Test file truncation logic to prevent OpenAI API limit errors."""

import json

import pytest
import pytest_bazel
import tiktoken

from x.inop.config import OptimizerConfig
from x.inop.engine.models import FileInfo
from x.inop.prompting.truncation_utils import TruncationManager


def _json_tokens(config: OptimizerConfig, files: list[FileInfo]) -> int:
    """Tokens of `files` as the prompt embeds them: the budget's unit."""
    encoding = tiktoken.encoding_for_model(config.grader.model)
    return len(encoding.encode(json.dumps([f.model_dump() for f in files], indent=2)))


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
        """Files over the budget are truncated until the result fits it."""
        files = [
            FileInfo(path=f"test_{i}.py", content=f"def function_{i}():\n    " + "print('test')\n" * 1000)
            for i in range(20)
        ]
        max_files_tokens = 4096
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, max_files_tokens)
        assert any("TRUNCATED" in f.content for f in result)
        final_tokens = _json_tokens(test_config, result)
        assert final_tokens <= max_files_tokens, f"Result exceeds limit: {final_tokens} > {max_files_tokens}"

    def test_largest_file_is_kept_ahead_of_smaller_ones(self, test_config):
        """The budget goes to the largest file first; the smaller one listed before it is dropped."""
        small = FileInfo(path="small.py", content="y = 2\n" * 500)
        large = FileInfo(path="large.py", content="x = 1\n" * 5000)
        budget = _json_tokens(test_config, [large]) // 2
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens([small, large], budget)
        assert [f.path for f in result] == ["large.py"]
        assert "TRUNCATED" in result[0].content

    def test_stops_adding_files_once_remaining_budget_is_below_minimum(self, test_config):
        """A leftover budget under the minimum is not spent on a sliver of the next file."""
        whole = FileInfo(path="whole.py", content="a = 1\n" * 2000)
        next_file = FileInfo(path="next.py", content="b = 2\n" * 1500)
        leftover = TruncationManager.MIN_TRUNCATION_BUDGET_TOKENS // 2
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens([whole, next_file], _json_tokens(test_config, [whole]) + leftover)
        assert result == [whole]

    def test_empty_files_list(self, test_config):
        """Empty input should return empty output."""
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens([], 150_000)
        assert result == []


if __name__ == "__main__":
    pytest_bazel.main()
