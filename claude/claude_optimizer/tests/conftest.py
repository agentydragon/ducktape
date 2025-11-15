"""Pytest configuration and shared fixtures."""

from collections.abc import Generator
from pathlib import Path
import tempfile

from claude_optimizer.config import OptimizerConfig
import pytest


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def test_config() -> OptimizerConfig:
    """Create a test configuration."""
    return OptimizerConfig(
        rollouts={"max_parallel": 2, "max_turns": 10, "bash_timeout_ms": 5000},
        prompt_engineer={"model": "gpt-4", "reasoning_effort": "low"},
        grader={"model": "gpt-4", "reasoning_effort": "low"},
        summarizer={"model": "gpt-4", "max_tokens": 1000},
        tokens={
            "max_response_tokens": 1000,
            "reasoning_buffer_tokens": 500,
            "max_context_tokens": 5000,
            "max_files_tokens": 2000,
        },
        truncation={"max_file_size_grading": 1000, "max_file_size_pattern_analysis": 1000, "log_message_length": 50},
        exclude_patterns=["*.log", ".git/", "*.pyc"],
    )


@pytest.fixture
def sample_task_yaml() -> str:
    """Sample task YAML content for testing."""
    return """
- id: test_task_001
  prompt: |
    Create a simple Python function that adds two numbers.
    Write working Python code to files.
  description: "Basic function creation test"
  docker_image: "claude-dev:python"
  allowed_tools: ["Read", "Write", "Edit"]
  pre_task_commands: null
"""


@pytest.fixture
def sample_grader_yaml() -> str:
    """Sample grader YAML content for testing."""
    return """
correctness:
  description: "Evaluates functional correctness of the solution"
  evaluation_criteria: |
    Check if the code produces correct output for given inputs.
    Look for proper error handling and edge cases.
"""
