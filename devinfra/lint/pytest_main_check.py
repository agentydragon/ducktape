"""Check py_test sources for a pytest entry point (invoked by a Bazel aspect)."""

from __future__ import annotations

import sys
from pathlib import Path


def check_sources(paths: list[Path]) -> list[str]:
    """Return diagnostics for sources without an accepted pytest entry point."""
    errors = []
    for path in paths:
        content = path.read_text()
        if "pytest_bazel.main()" not in content and "pytest.main(" not in content:
            errors.append(f"{path}: missing pytest_bazel.main() entry point")
    return errors


def main() -> int:
    report, *srcs = map(Path, sys.argv[1:])
    errors = check_sources(srcs)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    report.write_text("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
