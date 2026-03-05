"""Tests for approval_gate predicate system."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from approval_gate.predicates import Approved, Denied, NeedsHumanDecision, call_predicate, load_predicate


def test_load_predicate_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_predicate(tmp_path / "nonexistent.py")


def test_load_predicate_approved(tmp_path: Path):
    predicate_file = tmp_path / "predicate.py"
    predicate_file.write_text(
        "from approval_gate.predicates import Approved\n"
        "def decide(server_namespace, tool_name, arguments): return Approved()\n"
    )
    fn = load_predicate(predicate_file)
    result = fn("exec", "run", {"argv": ["echo", "hi"]})
    assert isinstance(result, Approved)


def test_load_predicate_denied(tmp_path: Path):
    predicate_file = tmp_path / "predicate.py"
    predicate_file.write_text(
        "from approval_gate.predicates import Denied\n"
        "def decide(server_namespace, tool_name, arguments): return Denied(reason='no')\n"
    )
    fn = load_predicate(predicate_file)
    result = fn("exec", "run", {})
    assert isinstance(result, Denied)
    assert result.reason == "no"


def test_load_predicate_conditional(tmp_path: Path):
    predicate_file = tmp_path / "predicate.py"
    predicate_file.write_text(
        "from approval_gate.predicates import Approved, NeedsHumanDecision\n"
        "def decide(server_namespace, tool_name, arguments):\n"
        "    if server_namespace == 'safe': return Approved()\n"
        "    return NeedsHumanDecision()\n"
    )
    fn = load_predicate(predicate_file)
    assert isinstance(fn("safe", "any_tool", {}), Approved)
    assert isinstance(fn("dangerous", "any_tool", {}), NeedsHumanDecision)


def test_load_predicate_syntax_error_raises(tmp_path: Path):
    predicate_file = tmp_path / "predicate.py"
    predicate_file.write_text("this is not valid python !!!")
    with pytest.raises(SyntaxError):
        load_predicate(predicate_file)


def test_call_predicate_catches_runtime_exception():
    def bad_predicate(server_namespace: str, tool_name: str, arguments: dict):
        raise RuntimeError("predicate crashed!")

    result = call_predicate(bad_predicate, "test", "tool", {})
    assert isinstance(result, NeedsHumanDecision)


def test_call_predicate_passes_through_approved():
    def always_approve(server_namespace: str, tool_name: str, arguments: dict):
        return Approved()

    result = call_predicate(always_approve, "test", "tool", {})
    assert isinstance(result, Approved)


def test_call_predicate_passes_through_denied():
    def always_deny(server_namespace: str, tool_name: str, arguments: dict):
        return Denied(reason="policy")

    result = call_predicate(always_deny, "test", "tool", {})
    assert isinstance(result, Denied)
    assert result.reason == "policy"


if __name__ == "__main__":
    pytest_bazel.main()
