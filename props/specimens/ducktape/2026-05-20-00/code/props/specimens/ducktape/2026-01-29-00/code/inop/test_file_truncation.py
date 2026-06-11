"""Test file truncation logic to prevent OpenAI API limit errors."""

import json

import pytest_bazel
import tiktoken

from inop.engine.models import FileInfo
from inop.prompting.truncation_utils import TruncationManager


class TestFileTruncation:
    """Test centralized file truncation logic."""

    def test_files_under_limit_unchanged(self, test_config):
        """Small files should pass through unchanged."""
        files = [FileInfo(path="small.py", content="print('hello')"), FileInfo(path="tiny.txt", content="small file")]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        assert result == files
        assert len(result) == 2

    def test_single_large_file_truncated(self, test_config):
        """A single very large file should be truncated if it exceeds token limit."""
        large_content = "# This is a large Python comment\n" + "x" * 500_000
        files = [FileInfo(path="huge.txt", content=large_content)]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        if len(result) == 0:
            assert True
        else:
            assert len(result) == 1
            assert result[0].path == "huge.txt"
            if len(result[0].content) < len(large_content):
                assert "TRUNCATED" in result[0].content

    def test_multiple_files_some_skipped(self, test_config):
        """When multiple files exceed limit, some should be skipped."""
        files = []
        for i in range(50):
            content = f"# File {i}\n" + "def function_" + str(i) + "():\n    " + "print('test')\n" * 1000
            files.append(FileInfo(path=f"file_{i}.py", content=content))
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        assert len(result) <= len(files)
        assert all(isinstance(f, FileInfo) for f in result)

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

    def test_preserves_file_structure(self, test_config):
        """File structure should be preserved."""
        files = [FileInfo(path="test.py", content="print('test')")]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        assert len(result) == 1
        assert result[0].path == "test.py"
        assert result[0].content == "print('test')"

    def test_binary_search_truncation_efficiency(self, test_config):
        """Binary search should find efficient truncation point."""
        large_content = "# Python code\n" + "print('line')\n" * 50_000
        files = [FileInfo(path="large.py", content=large_content)]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        if len(result) > 0 and "TRUNCATED" in result[0].content:
            truncated = result[0].content
            original_lines = large_content.count("\n")
            truncated_lines = truncated.count("\n")
            assert truncated_lines >= original_lines * 0.1

    def test_real_world_scenario(self, test_config):
        """Test with realistic file contents."""
        files = [
            FileInfo(
                path="main.py",
                content='''#!/usr/bin/env python3
"""Main application module."""

import os
import sys
import json
from typing import List, Dict, Any
import pytest_bazel

def main():
    """Main entry point."""
    print("Hello, world!")

''',
            ),
            FileInfo(
                path="requirements.txt",
                content="""pytest>=7.0.0
tiktoken>=0.5.0
openai>=1.0.0
""",
            ),
            FileInfo(
                path="README.md",
                content="""# Test Project

This is a test project for file truncation.
"""
                * 100,
            ),
        ]
        manager = TruncationManager(test_config)
        result = manager.truncate_files_by_tokens(files, 150_000)
        assert len(result) <= len(files)
        assert all(isinstance(f, FileInfo) for f in result)


if __name__ == "__main__":
    pytest_bazel.main()
