"""Tests for configuration management."""

import yaml

from inop.config import OptimizerConfig


def test_config_from_file(tmp_path):
    """Test loading config from YAML file."""
    config_data = {
        "rollouts": {"max_parallel": 4, "max_turns": 50, "bash_timeout_ms": 30000},
        "prompt_engineer": {"model": "gpt-4o", "reasoning_effort": "low"},
        "grader": {"model": "o3", "reasoning_effort": "high"},
        "summarizer": {"model": "gpt-4o", "max_tokens": 1000},
        "tokens": {
            "max_response_tokens": 1000,
            "reasoning_buffer_tokens": 500,
            "max_context_tokens": 5000,
            "max_files_tokens": 2000,
        },
        "truncation": {"max_file_size_grading": 1000, "max_file_size_pattern_analysis": 1000, "log_message_length": 50},
        "exclude_patterns": ["*.log", "*.tmp"],
    }

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config_data))

    config = OptimizerConfig.from_file(config_path)
    assert config.rollouts.max_parallel == 4
    assert "*.log" in config.exclude_patterns
