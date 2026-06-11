"""Environment helpers for wt.

Centralizes feature flags and test-mode detection.
"""

import os


def is_test_mode() -> bool:
    """Return True when running in test mode.

    Accepts common truthy values to avoid brittle string equality checks.
    Values considered true: "1", "true", "yes", "on" (case-insensitive).
    """
    v = os.environ.get("WT_TEST_MODE")
    if v is None:
        return False
    v = str(v).strip().lower()
    return v in {"1", "true", "yes", "on"}
