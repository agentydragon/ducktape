"""`Settings` construction: the deploy-config file source and the process-config cross-field checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_bazel

from haku.console.conftest import console_settings

# `console_settings` only stores the database URL, so these tests need no database.
_CONSOLE_DATABASE_URL = "postgresql+psycopg://app:secret@db.example.test:5432/haku"


def test_missing_deploy_config_fails_startup() -> None:
    with pytest.raises(RuntimeError, match=re.escape("/nonexistent/haku-console.yaml")):
        console_settings(_CONSOLE_DATABASE_URL, config_file=Path("/nonexistent/haku-console.yaml"))


if __name__ == "__main__":
    pytest_bazel.main()
