"""The console's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.haku.charts import console_chart


@pytest.fixture(scope="session")
def haku_console_manifests() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Testing.synth(console_chart(Testing.app())))
