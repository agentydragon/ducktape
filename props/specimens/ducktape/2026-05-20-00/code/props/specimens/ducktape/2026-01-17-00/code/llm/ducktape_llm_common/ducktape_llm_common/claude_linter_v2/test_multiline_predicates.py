"""Test multiline predicate evaluation."""

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from ducktape_llm_common.claude_linter_v2.access.context import PredicateContext
from ducktape_llm_common.claude_linter_v2.access.evaluator import PredicateEvaluator
from ducktape_llm_common.claude_linter_v2.types import SessionID

# Test session ID using valid UUID format
TEST_SESSION_ID = SessionID(UUID("00000000-0000-0000-0000-000000000001"))


class TestMultilinePredicates:
    """Test complex multiline predicate evaluation."""

    @pytest.fixture
    def evaluator(self):
        """Create evaluator instance."""
        return PredicateEvaluator()

    @pytest.fixture
    def context(self):
        """Create test context."""
        return PredicateContext(
            tool="Bash",
            args={"file_path": "/home/user/test.py", "content": "print('hello')", "command": "grep -r pattern"},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )

    def test_simple_multiline_function(self, evaluator, context):
        """Test basic multiline function."""
        predicate = """
def check_bash(ctx):
    return ctx.tool == "Bash"
"""
        assert evaluator.evaluate(predicate, context) is True

        # Change tool
        context_edit = PredicateContext(
            tool="Edit",
            args={"file_path": "/home/user/test.py", "content": "print('hello')", "command": "grep -r pattern"},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, context_edit) is False

    def test_complex_shell_pipeline_check(self, evaluator, context):
        """Test complex shell pipeline validation."""
        predicate = """
import shlex

def is_safe_pipeline(ctx):
    if ctx.tool != "Bash" or not ctx.command:
        return False

    SAFE_COMMANDS = {
        "grep": {"-r", "-i", "-n", "-v", "-E"},
        "find": {"-name", "-type", "-path"},
        "cat": set(),
        "wc": {"-l", "-w", "-c"},
    }

    # Parse pipeline
    parts = ctx.command.split("|")
    for part in parts:
        part = part.strip()
        if not part:
            continue

        try:
            tokens = shlex.split(part)
            if not tokens:
                continue

            cmd = tokens[0]
            if cmd not in SAFE_COMMANDS:
                return False

            # Check flags
            allowed_flags = SAFE_COMMANDS[cmd]
            for token in tokens[1:]:
                if token.startswith("-") and token not in allowed_flags:
                    return False

        except ValueError:
            return False

    return True
"""
        # Safe pipeline
        context_safe = PredicateContext(
            tool="Bash",
            args={"command": "grep -r pattern | wc -l"},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, context_safe) is True

        # Unsafe command
        context_unsafe = PredicateContext(
            tool="Bash", args={"command": "rm -rf /"}, session_id=TEST_SESSION_ID, timestamp=datetime.now()
        )
        assert evaluator.evaluate(predicate, context_unsafe) is False

        # Unknown flag
        context_bad_flag = PredicateContext(
            tool="Bash", args={"command": "grep -X pattern"}, session_id=TEST_SESSION_ID, timestamp=datetime.now()
        )
        assert evaluator.evaluate(predicate, context_bad_flag) is False

    def test_domain_specific_mcp_check(self, evaluator):
        """Test domain-specific MCP tool validation (stock broker example)."""
        predicate = """
def check_broker_limits(ctx):
    # Stock broker MCP safety check
    MAX_ACCOUNT_VALUE = 500
    MAX_MARGIN = 5

    if not ctx.tool.startswith("mcp_broker_"):
        return True  # Not a broker tool

    # Check account value limit
    if "amount" in ctx.args:
        if ctx.args["amount"] > MAX_ACCOUNT_VALUE:
            return False

    # Check margin limit
    if "margin_multiplier" in ctx.args:
        if ctx.args["margin_multiplier"] > MAX_MARGIN:
            return False

    # Check for forbidden operations
    forbidden_ops = ["withdraw", "transfer", "close_account"]
    if any(op in ctx.tool for op in forbidden_ops):
        return False

    return True
"""
        # Create context for broker MCP
        broker_context = PredicateContext(
            tool="mcp_broker_place_order",
            args={"amount": 100, "margin_multiplier": 2},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, broker_context) is True

        # Exceed amount limit
        broker_context_high_amount = PredicateContext(
            tool="mcp_broker_place_order",
            args={"amount": 1000, "margin_multiplier": 2},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, broker_context_high_amount) is False

        # Exceed margin limit
        broker_context_high_margin = PredicateContext(
            tool="mcp_broker_place_order",
            args={"amount": 100, "margin_multiplier": 10},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, broker_context_high_margin) is False

        # Forbidden operation
        broker_context_forbidden = PredicateContext(
            tool="mcp_broker_withdraw",
            args={"amount": 100, "margin_multiplier": 2},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, broker_context_forbidden) is False

    def test_result_variable_style(self, evaluator, context):
        """Test using result variable instead of expression."""
        predicate = """
def check_test_file(ctx):
    import pathlib
    # Check if editing Python test files only
    if ctx.tool == "Edit" and ctx.path:
        path = pathlib.Path(ctx.path)
        result = path.suffix == ".py" and "test" in path.name
    else:
        result = False
    return result
"""
        context_edit_test = PredicateContext(
            tool="Edit",
            args={"file_path": "/home/user/test_foo.py"},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now(),
        )
        assert evaluator.evaluate(predicate, context_edit_test) is True

        context_edit_non_test = PredicateContext(
            tool="Edit", args={"file_path": "/home/user/foo.py"}, session_id=TEST_SESSION_ID, timestamp=datetime.now()
        )
        assert evaluator.evaluate(predicate, context_edit_non_test) is False

    def test_imports_and_modules(self, evaluator, context):
        """Test that imports work correctly."""
        predicate = """
import json
import re
from datetime import datetime, timedelta

def check_recent_activity(ctx):
    # Check if activity is within last hour
    now = datetime.now()
    one_hour_ago = now - timedelta(hours=1)
    return ctx.timestamp > one_hour_ago
"""
        # Recent timestamp
        context_recent = PredicateContext(
            tool="Bash", args={"command": "test"}, session_id=TEST_SESSION_ID, timestamp=datetime.now()
        )
        assert evaluator.evaluate(predicate, context_recent) is True

        # Old timestamp
        context_old = PredicateContext(
            tool="Bash",
            args={"command": "test"},
            session_id=TEST_SESSION_ID,
            timestamp=datetime.now() - timedelta(hours=2),
        )
        assert evaluator.evaluate(predicate, context_old) is False

    def test_error_handling(self, evaluator, context):
        """Test error handling in multiline predicates."""
        # Missing return - function returns None which is falsy but valid
        predicate = """
def check(ctx):
    if ctx.tool == "Bash":
        pass  # Returns None implicitly
"""
        # This should not raise an error - None is valid and evaluates to False
        result = evaluator.evaluate(predicate, context)
        assert result is False  # None is falsy

        # Syntax error
        predicate = """
def check(ctx:
    return True
"""
        with pytest.raises(ValueError, match="Invalid Python syntax"):
            evaluator.evaluate(predicate, context)

    def test_mixed_single_and_multiline(self, evaluator, context):
        """Test that both simple and complex predicates work."""
        # Simple function
        predicate_simple = """
def check_tool(ctx):
    return ctx.tool == 'Bash'
"""
        assert evaluator.evaluate(predicate_simple, context) is True

        # More complex function
        predicate_git = """
def safe_git_commands(ctx):
    if ctx.tool != "Bash":
        return False
    if not ctx.command:
        return False
    safe_cmds = ["git status", "git log", "git diff"]
    return any(ctx.command.startswith(cmd) for cmd in safe_cmds)
"""
        assert evaluator.evaluate(predicate_git, context) is False  # Not a git command

        # Test with git command
        context_git = PredicateContext(
            tool="Bash", args={"command": "git status"}, session_id=TEST_SESSION_ID, timestamp=datetime.now()
        )
        assert evaluator.evaluate(predicate_git, context_git) is True
