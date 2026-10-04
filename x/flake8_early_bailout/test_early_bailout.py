"""Tests for flake8-early-bailout plugin."""

import ast
import textwrap

import pytest_bazel

from x.flake8_early_bailout.flake8_early_bailout import EarlyBailoutChecker


def get_errors(code: str) -> list[str]:
    """Get error messages from the checker."""
    tree = ast.parse(textwrap.dedent(code))
    checker = EarlyBailoutChecker(tree)
    errors = list(checker.run())
    return [f"{line}:{col} {msg}" for line, col, msg, _ in errors]


def test_inverted_if_else():
    """Test detection of if/else that should be inverted."""
    code = """
    def process_data(data):
        if data is not None:
            # Process the data
            result = data.strip()
            normalized = result.lower()
            tokens = normalized.split()
            processed = [t for t in tokens if t]
            return processed
        else:
            return []
    """
    errors = get_errors(code)
    assert len(errors) == 1
    assert "EB100" in errors[0]
    assert "invert condition and return early" in errors[0]
    print("✓ Detected inverted if/else pattern")


def test_deeply_nested_ifs():
    """Test detection of nested if statements."""
    code = """
    def process_request(request):
        if request.is_valid():
            data = request.get_data()
            processed = preprocess(data)
            if data:
                user = data.get('user')
                validated = validate(user)
                if user:
                    result = process_user(user)
                    logged = log_access(result)
                    return result
    """
    errors = get_errors(code)
    assert len(errors) >= 1
    assert any("EB101" in error for error in errors)
    print("✓ Detected nested if pattern")


def test_no_error_for_balanced_if_else():
    """Test no error when if/else are balanced."""
    code = """
    def choose_path(condition):
        if condition:
            # Path A
            do_something()
            do_another_thing()
        else:
            # Path B
            do_different_thing()
            do_yet_another_thing()
    """
    errors = get_errors(code)
    assert len(errors) == 0
    print("✓ No error for balanced if/else")


def test_loop_with_inverted_pattern():
    """Test detection in loop context."""
    code = """
    def process_items(items):
        for item in items:
            if item.is_valid():
                # Long processing logic
                cleaned = item.strip()
                normalized = cleaned.lower()
                result = transform(normalized)
                output = format_result(result)
                save(output)
            else:
                log_error(item)
    """
    errors = get_errors(code)
    assert len(errors) == 1
    assert "EB100" in errors[0]
    assert "continue or break" in errors[0]
    print("✓ Detected inverted pattern in loop")


if __name__ == "__main__":
    test_inverted_if_else()
    test_deeply_nested_ifs()
    test_no_error_for_balanced_if_else()
    test_loop_with_inverted_pattern()
    print("\nAll tests passed!")


if __name__ == "__main__":
    pytest_bazel.main()
