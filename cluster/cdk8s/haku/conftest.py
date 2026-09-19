"""Each console directory's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.haku.charts import DIRECTORIES, chart


@pytest.fixture(scope="session")
def haku_console_manifests() -> dict[str, list[dict[str, Any]]]:
    return {
        directory.name: cast(list[dict[str, Any]], Testing.synth(chart(Testing.app(), directory)))
        for directory in DIRECTORIES
    }
