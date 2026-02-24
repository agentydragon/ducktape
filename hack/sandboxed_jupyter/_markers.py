import platform

import pytest

# Shared pytest markers for platform-specific test gating.

MACOS_ONLY = pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only test")

REQUIRES_SANDBOX_EXEC = (pytest.mark.requires_sandbox_exec, MACOS_ONLY, pytest.mark.macos)
