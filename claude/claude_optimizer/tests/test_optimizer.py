"""Tests for the Claude instruction optimizer."""

from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from claude_optimizer.config import OptimizerConfig
from claude_optimizer.core.jsonl_logger import JSONLLogger, safe_serialize
from claude_optimizer.core.logging_openai_client import LoggingOpenAIClient, LoggingOpenAIModel
from claude_optimizer.core.message_formatter import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    log_message_summary,
)
from claude_optimizer.core.models import CodeResult, Grade, GradedCode, ScoreWithRationale
from claude_optimizer.core.optimizer import (
    ProcessingMode,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallItem,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
)
from claude_optimizer.core.prompt_engineer import PromptEngineer, Turn
from claude_optimizer.core.summarizer import PatternSummarizer
from claude_optimizer.docker.docker_manager import DockerManager
from openai.types.responses.response import Response
import pydantic
import pytest
import yaml


class TestPatternSummarizer:
    """Test pattern summarization functionality."""

    def mock_test_config(self) -> OptimizerConfig:
        return OptimizerConfig(
            rollouts={"max_parallel": 2, "max_turns": 10, "bash_timeout_ms": 5000},
            prompt_engineer={"model": "gpt-4o", "reasoning_effort": "low"},
            grader={"model": "o3", "reasoning_effort": "high"},
            summarizer={"model": "gpt-4o", "max_tokens": 1000},
            tokens={
                "max_response_tokens": 1000,
                "reasoning_buffer_tokens": 500,
                "max_context_tokens": 5000,
                "max_files_tokens": 150000,
            },
            truncation={
                "max_file_size_grading": 100000,
                "max_file_size_pattern_analysis": 100000,
                "log_message_length": 200,
            },
            exclude_patterns=["*.log", ".git/", "*.pyc", "__pycache__"],
        )

    @pytest.mark.asyncio
    async def test_summarize_patterns_basic(self, tmp_path):
        """Test basic pattern summarization."""
        cfg = self.mock_test_config()
        summarizer = PatternSummarizer(cfg, JSONLLogger(tmp_path / "openai_log.jsonl"))

        # Create mock rollout results
        mock_results = [
            create_mock_graded_code(
                task="implement sorting",
                overall_score=7.5,
                axes={
                    "type_safety": ScoreWithRationale(score=8, rationale="Good typing"),
                    "robustness": ScoreWithRationale(score=6, rationale="Needs error handling"),
                },
            )
        ]

        with patch("claude_optimizer.core.summarizer.OpenAI") as mock_openai:
            mock_client = Mock()
            mock_openai.return_value = mock_client

            mock_response = create_mock_pattern_response("Common issues found: lack of error handling")
            mock_client.responses.create.return_value = mock_response

            result = await summarizer.summarize_patterns(mock_results)

            assert result == "Common issues found: lack of error handling"
            assert mock_client.responses.create.called

    @pytest.mark.asyncio
    async def test_summarize_patterns_multiple_tasks(self, tmp_path):
        """Test pattern summarization with multiple tasks."""
        cfg = self.mock_test_config()
        summarizer = PatternSummarizer(cfg, JSONLLogger(tmp_path / "log.jsonl"))

        mock_results = [
            create_mock_graded_code("task1", 8.0, {"type_safety": ScoreWithRationale(score=9, rationale="Excellent")}),
            create_mock_graded_code("task2", 5.0, {"type_safety": ScoreWithRationale(score=4, rationale="Poor")}),
            create_mock_graded_code("task3", 6.5, {"type_safety": ScoreWithRationale(score=7, rationale="Good")}),
        ]

        with patch("claude_optimizer.core.summarizer.OpenAI") as mock_openai:
            mock_client = Mock()
            mock_openai.return_value = mock_client

            mock_response = create_mock_pattern_response("Mixed results across tasks")
            mock_client.responses.create.return_value = mock_response

            await summarizer.summarize_patterns(mock_results)

            # Verify the call included all tasks
            call_args = mock_client.responses.create.call_args
            assert call_args is not None
            request_data = call_args[1] if call_args[1] else call_args[0]
            assert "Task 1:" in str(request_data)
            assert "Task 2:" in str(request_data)
            assert "Task 3:" in str(request_data)


class TestPromptEngineer:
    """Test prompt engineering conversation management."""

    def test_initialization_full_rollouts(self):
        """Test PromptEngineer initialization with full rollouts mode."""
        cfg = TestPatternSummarizer().mock_test_config()
        engineer = PromptEngineer(cfg, JSONLLogger(Path("/dev/null")), ProcessingMode.FULL_ROLLOUTS)

        assert len(engineer._turns) == 0
        assert engineer._processing_mode == ProcessingMode.FULL_ROLLOUTS
        assert "prompt engineer" in engineer._system_message["content"]
        assert "analyze rollouts from coding tasks" in engineer._system_message["content"]

    def test_initialization_summary_mode(self):
        """Test PromptEngineer initialization with summary mode."""
        cfg = TestPatternSummarizer().mock_test_config()
        engineer = PromptEngineer(cfg, JSONLLogger(Path("/dev/null")), ProcessingMode.SUMMARY)

        assert engineer._processing_mode == ProcessingMode.SUMMARY
        assert "pattern summaries and insights" in engineer._system_message["content"]

    def test_context_trimming(self):
        """Test that context is trimmed when exceeding token limit."""
        cfg = TestPatternSummarizer().mock_test_config()
        engineer = PromptEngineer(cfg, JSONLLogger(Path("/dev/null")))

        # Add many turns to exceed token limit
        for i in range(10):
            engineer.add_result(
                reasoning=[create_mock_reasoning(f"Reasoning {i}")],
                function_call_message=create_mock_function_call(f"prompt_{i}"),
                proposed_prompt=f"System prompt version {i}" * 1000,  # Long prompt
                grades=f"Grade results {i}" * 1000,  # Long grades
            )

        # Force trimming with low token limit
        engineer._trim_context_if_needed(max_tokens=1000)

        # Should keep only last 2 turns
        assert len(engineer._turns) == 2

    def test_build_grades_message(self):
        """Test building grades message from rollout results."""
        cfg = TestPatternSummarizer().mock_test_config()
        engineer = PromptEngineer(cfg, JSONLLogger(Path("/dev/null")))

        mock_results = [
            create_mock_graded_code(
                "implement API client", 7.0, {"architecture": ScoreWithRationale(score=8, rationale="Clean separation")}
            ),
            create_mock_graded_code(
                "build parser", 9.0, {"correctness": ScoreWithRationale(score=10, rationale="Perfect implementation")}
            ),
        ]

        message = engineer.build_grades_message(mock_results)

        assert "testing the current system prompt on 2 coding tasks" in message
        assert "implement API client" in message
        assert "build parser" in message
        assert "Overall Grade: 7.0" in message
        assert "Overall Grade: 9.0" in message

    @pytest.mark.asyncio
    async def test_propose_prompt(self, tmp_path):
        """Test prompt proposal generation."""
        cfg = TestPatternSummarizer().mock_test_config()
        engineer = PromptEngineer(cfg, JSONLLogger(Path("/dev/null")))

        # Add initial context
        mock_results = [create_mock_graded_code("test task", 8.0, {})]
        grades_message = engineer.build_grades_message(mock_results)

        engineer._turns.append(
            Turn(
                reasoning=[],
                function_call_message=create_mock_function_call("initial"),
                proposed_prompt="Initial prompt",
                grades={"text": grades_message},
            )
        )

        with patch("claude_optimizer.core.prompt_engineer.OpenAI") as mock_openai:
            mock_client = Mock()
            mock_openai.return_value = mock_client

            reasoning_msg = ResponseOutputMessage.model_construct(
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText.model_construct(
                        type="output_text", text="Analyzing the results...", annotations=[]
                    )
                ],
                id="msg_1",
                status="completed",
            )
            fc_item = ResponseFunctionToolCallItem.model_construct(
                type="function_call",
                name="submit_prompt",
                call_id="call_1",
                arguments=json.dumps({"prompt": "Improved system prompt"}),
                id="fc_1",
                status="completed",
            )
            resp = Response.model_construct(output=[reasoning_msg, fc_item])
            mock_client.responses.create.return_value = resp

            reasoning, function_call, prompt = await engineer.propose_prompt()

            assert len(reasoning) == 1
            assert prompt == "Improved system prompt"
            assert function_call.name == "submit_prompt"


class TestOptimizerConfig:
    """Test configuration management."""

    def test_config_from_file_like(self, tmp_path):
        """Test loading config minimal valid YAML using from_file."""
        cfg_data = {
            "rollouts": {"max_parallel": 2, "max_turns": 10, "bash_timeout_ms": 5000},
            "prompt_engineer": {"model": "gpt-4o", "reasoning_effort": "low"},
            "grader": {"model": "o3", "reasoning_effort": "high"},
            "summarizer": {"model": "gpt-4o", "max_tokens": 1000},
            "tokens": {
                "max_response_tokens": 1000,
                "reasoning_buffer_tokens": 500,
                "max_context_tokens": 5000,
                "max_files_tokens": 2000,
            },
            "truncation": {
                "max_file_size_grading": 1000,
                "max_file_size_pattern_analysis": 1000,
                "log_message_length": 50,
            },
            "exclude_patterns": ["*.log", "*.tmp"],
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_data))
        cfg = OptimizerConfig.from_file(cfg_path)
        assert cfg.rollouts.max_parallel == 2
        assert cfg.prompt_engineer.model == "gpt-4o"
        assert "*.log" in cfg.exclude_patterns

    def test_config_validation(self):
        """Test configuration validation."""
        with pytest.raises(pydantic.ValidationError):
            OptimizerConfig(invalid_field="value")


class TestDockerManager:
    """Test Docker management functionality."""

    def test_docker_manager_init(self):
        """Test DockerManager initialization."""
        with patch("shutil.which", return_value="/usr/bin/docker"):
            manager = DockerManager()
            assert manager.docker_path == "/usr/bin/docker"
            assert not hasattr(manager, "is_setup")

    def test_docker_not_found(self):
        """Test error when Docker is not found."""
        with patch("shutil.which", return_value=None), pytest.raises(RuntimeError, match="Docker is required"):
            DockerManager()

    def test_setup_wrapper(self, tmp_path):
        """Test Docker wrapper setup via context manager."""
        with patch("shutil.which", return_value="/usr/bin/docker"):
            manager = DockerManager()
            original_path = os.environ.get("PATH", "")

            # Create mock wrapper script path (existence required)
            wrapper_source = tmp_path / "docker_claude_wrapper.sh"
            wrapper_source.touch()

            with manager.wrapper(tmp_path, wrapper_source) as isolated_path:
                wrapper_path = tmp_path / "bin" / "claude"
                assert wrapper_path.exists()
                assert isolated_path == str(tmp_path / "bin")
                # Global PATH should remain unchanged; subprocess env is provided via get_subprocess_env
                assert os.environ.get("PATH", "") == original_path

            # After context exit, wrapper should be cleaned up
            assert not (tmp_path / "bin" / "claude").exists()

    def test_cleanup(self, tmp_path):
        """Wrapper context cleans up and does not mutate global PATH."""
        original_path = os.environ.get("PATH", "")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            manager = DockerManager()
            wrapper_source = tmp_path / "wrapper.sh"
            wrapper_source.touch()

            with manager.wrapper(tmp_path, wrapper_source) as isolated_path:
                assert (tmp_path / "bin" / "claude").exists()
                env = manager.get_subprocess_env("container123", isolated_path)
                assert env["PATH"] == isolated_path
                assert env["CLAUDE_CONTAINER_ID"] == "container123"
                # Global PATH remains unchanged
                assert os.environ.get("PATH", "") == original_path

            # After exiting context, wrapper file is removed
            assert not (tmp_path / "bin" / "claude").exists()
            # Global PATH unchanged
            assert os.environ.get("PATH", "") == original_path


class TestHelperFunctions:
    """Test helper functions."""

    def test_logging_openai_model(self):
        """Test LoggingOpenAIModel wraps OpenAI client and logs."""
        client = LoggingOpenAIClient(openai_client=Mock(), jsonl_logger=JSONLLogger(Path("/dev/null")))
        model = LoggingOpenAIModel(
            openai_client=client, model="o3", context_window_tokens=8192, reasoning_effort="high"
        )

        # Ensure attributes are set correctly
        assert model.model == "o3"
        assert model.reasoning_effort == "high"

    def test_log_openai_request_response(self, tmp_path):
        """Test OpenAI API logging."""
        log_path = tmp_path / "openai_log.jsonl"

        request = {"model": "o3", "input": []}
        response = Mock()
        response.model_dump.return_value = {"output": "test"}

        logger = JSONLLogger(log_path)
        logger.log(request=request, response=safe_serialize(response))

        # Verify log was written
        assert log_path.exists()
        with log_path.open() as f:
            log_entry = json.loads(f.readline())
            assert log_entry["request"] == request
            assert log_entry["response"]["output"] == "test"
            assert "timestamp" in log_entry

    def test_log_anthropic_request_event(self, tmp_path):
        """Test Anthropic API logging."""
        log_path = tmp_path / "anthropic_log.jsonl"

        request = {"prompt": "test", "options": {}}
        event = "test_event"

        logger = JSONLLogger(log_path)
        logger.log(request=request, event=event)

        assert log_path.exists()
        with log_path.open() as f:
            log_entry = json.loads(f.readline())
            assert log_entry["request"] == request
            assert log_entry["event"] == "test_event"


class TestMessageLogging:
    """Test message logging functionality."""

    def test_log_system_message(self, caplog):
        """Test logging of system messages."""

        msg = SystemMessage(subtype="test_subtype", data={})
        with patch("claude_optimizer.core.optimizer.logger") as mock_logger:
            log_message_summary(msg, logger=mock_logger, agent_id=1)

            # Verify logger was called correctly
            mock_logger.bind.assert_called_with(agent_id=1, message_type="SystemMessage")

    def test_log_assistant_message_with_tools(self, caplog):
        """Test logging of assistant messages with tool usage."""

        msg = AssistantMessage(
            content=[TextBlock(text="Using tool"), ToolUseBlock(id="123", name="test_tool", input={"param": "value"})]
        )

        with patch("claude_optimizer.core.optimizer.logger") as mock_logger:
            mock_logger.bind.return_value = mock_logger
            log_message_summary(msg, logger=mock_logger, agent_id=2)

            # Should log tool usage
            mock_logger.info.assert_called()
            call_args = [call[0][0] for call in mock_logger.info.call_args_list]
            assert "Tool usage" in call_args


# Helper functions for creating mock objects
def create_mock_graded_code(task: str, overall_score: float, axes: dict[str, ScoreWithRationale]) -> GradedCode:
    """Create a mock GradedCode object for testing."""
    return GradedCode(
        code_result=CodeResult(
            task=task,
            task_id="test_task",
            agent_id=1,
            timestamp=datetime.utcnow(),
            messages=[],
            files=[{"path": "test.py", "content": "# test code"}],
        ),
        grade=Grade(
            task=task,
            task_id="test_task",
            agent_id=1,
            axes=axes,
            overall_score=overall_score,
            overall_rationale="Test rationale",
            timestamp=datetime.utcnow(),
        ),
    )


def create_mock_pattern_response(text: str):
    """Create a mock OpenAI response for pattern analysis."""

    msg = ResponseOutputMessage.model_construct(
        type="message",
        role="assistant",
        content=[ResponseOutputText.model_construct(type="output_text", text=text, annotations=[])],
        id="msg_1",
        status="completed",
    )
    return Response.model_construct(output=[msg])


def create_mock_reasoning(content: str):
    """Create mock reasoning item."""

    mock = Mock(spec=ResponseReasoningItem)
    mock.content = content
    mock.model_dump.return_value = {"content": content}
    return mock


def create_mock_function_call(prompt: str):
    """Create mock function call message."""

    mock = Mock(spec=ResponseFunctionToolCall)
    mock.name = "submit_prompt"
    mock.arguments = json.dumps({"prompt": prompt})
    mock.call_id = "call_123"
    mock.model_dump.return_value = {"name": "submit_prompt", "arguments": mock.arguments, "call_id": "call_123"}
    return mock


def create_mock_function_call_item(name: str, args: dict):
    """Create mock function call item."""
    mock = Mock(spec=ResponseFunctionToolCallItem)
    mock.type = "function_call"
    mock.name = name
    mock.arguments = json.dumps(args)
    mock.call_id = "call_456"
    return mock


# Fixtures
@pytest.fixture
def mock_openai_client():
    """Provide a mock OpenAI client."""
    with patch("claude_optimizer.core.optimizer.OpenAI") as mock:
        yield mock.return_value


@pytest.fixture
def mock_config():
    """Provide a test configuration."""
    return OptimizerConfig(max_parallel_rollouts=2, bash_timeout_ms=5000, truncation_length=50)
