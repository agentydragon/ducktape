"""Pytest runner for gatelet tests.

Sets GATELET_CONFIG environment variable before any imports,
then delegates to pytest_bazel.main() for Bazel integration.
"""

import os

# Set config path BEFORE any gatelet imports
# (prevents import-time config reads from failing)
os.environ.setdefault("GATELET_CONFIG", "gatelet/gatelet.toml")

import pytest_bazel

if __name__ == "__main__":
    pytest_bazel.main()
