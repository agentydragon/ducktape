"""Lexical pytest entry-point checks shared by the hook and CI preflight."""

from __future__ import annotations


def has_pytest_bazel_main(content: str) -> bool:
    return "pytest_bazel.main()" in content


def has_pytest_main(content: str) -> bool:
    return "pytest.main(" in content


def has_unconditional_pytest_main_pass(content: str) -> bool:
    return has_pytest_bazel_main(content) or has_pytest_main(content)
