import pytest

# Prevent pytest from collecting test files inside snapshot code/ directories.
# Specimen code is frozen third-party data, not our test suite.
collect_ignore_glob = ["*"]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command-line options for specimen test parameters."""
    parser.addoption("--slug", action="store", required=False, help="Specimen slug (repo/date)")
    parser.addoption("--code-tar", action="store", required=False, help="Path to specimen code tar.gz")
    parser.addoption("--issues-dir", action="store", required=False, help="Specimen package path for issues/")
