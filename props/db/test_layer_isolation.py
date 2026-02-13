"""Test that database layer is properly isolated from grader layer.

The database persistence layer should not depend on grader layer to avoid
coupling database migrations to grader-specific logic.
"""

import ast
from pathlib import Path

import pytest_bazel


def test_db_does_not_import_grader():
    """Verify that db/ modules do not import from grader.*."""
    db_dir = Path(__file__).parent
    db_files = list(db_dir.glob("*.py"))

    violations = []

    for file_path in db_files:
        if file_path.name.startswith("_"):
            continue

        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "grader" in node.module:
                    violations.append(f"{file_path.name}: imports from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "grader" in alias.name:
                        violations.append(f"{file_path.name}: imports {alias.name}")

    if violations:
        msg = "Database layer must not import from grader modules.\n\nViolations found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
        raise AssertionError(msg)


if __name__ == "__main__":
    pytest_bazel.main()
