import re
from pathlib import Path

import pytest_bazel

from wt.testing.conftest import get_wt_package_dir

WT_DIR = get_wt_package_dir()

BANNED_PATTERNS = {
    r"\bgetattr\b": "getattr is banned; use explicit attribute access with proper types",
    r"\bhasattr\b": "hasattr is banned; use explicit attribute access with proper types",
    r"\bsetattr\b": "setattr is banned; avoid dynamic attributes",
    r"\bos\.path\.": "os.path is banned; use pathlib.Path",
    r"except\s*:": "bare except is banned; catch specific exceptions",
}

EXCLUDE_DIRS = {".benchmarks", "wt.egg-info", "tests", "testing"}


def iter_python_files(base: Path):
    for p in base.rglob("*.py"):
        parts = set(p.parts)
        if parts & EXCLUDE_DIRS:
            continue
        # Exclude test files (which may define banned patterns as test data)
        if p.name.startswith("test_"):
            continue
        yield p


def test_banned_patterns_absent():
    failures: list[str] = []
    for path in iter_python_files(WT_DIR):
        text = path.read_text(errors="ignore")
        for pattern, message in BANNED_PATTERNS.items():
            for m in re.finditer(pattern, text):
                # Allow docstrings or comments to mention patterns without code usage
                line = text.count("\n", 0, m.start()) + 1
                # Very rough filter: ignore occurrences in strings/comments
                before = text.rfind("\n", 0, m.start()) + 1
                snippet = text[before : m.start()].strip()
                if snippet.startswith("#"):
                    continue
                failures.append(f"{path}:{line}: {message}")
    assert not failures, "\n" + "\n".join(failures)


if __name__ == "__main__":
    pytest_bazel.main()
