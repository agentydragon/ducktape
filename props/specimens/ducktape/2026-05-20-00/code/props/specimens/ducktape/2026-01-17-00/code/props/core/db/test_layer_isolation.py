"""Test that database layer is properly isolated from grader layer.

The database persistence layer should not depend on grader layer to avoid
coupling database migrations to grader-specific logic.
"""

import ast
from pathlib import Path


def test_db_does_not_import_grader():
    """Verify that db/ modules do not import from grader.*.

    The database layer uses db.snapshots (DBTruePositiveIssue, DBKnownFalsePositive)
    and should not import from grader modules. Conversion between layers happens
    in grader.persistence.
    """
    db_dir = Path(__file__).parent.parent.parent / "src" / "props_core" / "db"
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
        msg = (
            "Database layer must not import from grader modules.\n"
            "Use db.snapshots models instead (DBTruePositiveIssue, DBKnownFalsePositive).\n"
            "Conversions should live in grader.persistence.\n\n"
            "Violations found:\n" + "\n".join(f"  - {v}" for v in violations)
        )
        raise AssertionError(msg)
