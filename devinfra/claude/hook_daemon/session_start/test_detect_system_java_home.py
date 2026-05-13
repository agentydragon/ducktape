"""Tests for system JDK detection used by the session start hook.

The detection drives both --server_javabase in the session bazelrc and the
JAVA_HOME export in the env file, so it must be:

  * permissive (any /usr/lib/jvm/...-openjdk-* path with a working bin/java)
  * fail-closed (no false positives — a directory missing bin/java is None)
  * NixOS-safe (no /usr/lib/jvm/... at all returns None, falling back to the
    bundled JDK)
"""

from pathlib import Path
from unittest.mock import patch

import pytest_bazel

from devinfra.claude.hook_daemon.session_start import handler


def test_detects_first_candidate_with_executable_java(tmp_path: Path) -> None:
    """The first candidate with an executable bin/java wins."""
    jdk = tmp_path / "java-21-openjdk-amd64"
    bin_dir = jdk / "bin"
    bin_dir.mkdir(parents=True)
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexec true\n")
    java.chmod(0o755)

    with patch.object(handler, "_SYSTEM_JAVA_HOME_CANDIDATES", (jdk,)):
        assert handler._detect_system_java_home() == jdk


def test_returns_none_when_no_candidate_exists(tmp_path: Path) -> None:
    """NixOS / CLI hosts without /usr/lib/jvm: fall back to bundled JDK."""
    missing = tmp_path / "does-not-exist"
    with patch.object(handler, "_SYSTEM_JAVA_HOME_CANDIDATES", (missing,)):
        assert handler._detect_system_java_home() is None


def test_skips_directory_without_executable_java(tmp_path: Path) -> None:
    """A JDK directory with non-executable java is not usable."""
    jdk = tmp_path / "broken-jdk"
    bin_dir = jdk / "bin"
    bin_dir.mkdir(parents=True)
    java = bin_dir / "java"
    java.write_text("# not executable")
    java.chmod(0o644)

    with patch.object(handler, "_SYSTEM_JAVA_HOME_CANDIDATES", (jdk,)):
        assert handler._detect_system_java_home() is None


def test_prefers_earlier_candidate(tmp_path: Path) -> None:
    """When multiple candidates exist, the first one in the tuple wins."""
    preferred = tmp_path / "java-21-openjdk-amd64"
    fallback = tmp_path / "java-11-openjdk-amd64"
    for jdk in (preferred, fallback):
        (jdk / "bin").mkdir(parents=True)
        java = jdk / "bin" / "java"
        java.write_text("#!/bin/sh\nexec true\n")
        java.chmod(0o755)

    with patch.object(handler, "_SYSTEM_JAVA_HOME_CANDIDATES", (preferred, fallback)):
        assert handler._detect_system_java_home() == preferred


if __name__ == "__main__":
    pytest_bazel.main()
