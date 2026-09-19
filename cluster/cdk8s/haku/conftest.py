"""Each console directory's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.haku.charts import CONSOLE, DB, MIGRATION, console_chart, db_chart, migration_chart


@pytest.fixture(scope="session")
def haku_console_manifests() -> dict[str, list[dict[str, Any]]]:
    return {
        DB.name: cast(list[dict[str, Any]], Testing.synth(db_chart(Testing.app()))),
        MIGRATION.name: cast(list[dict[str, Any]], Testing.synth(migration_chart(Testing.app()))),
        CONSOLE.name: cast(list[dict[str, Any]], Testing.synth(console_chart(Testing.app()))),
    }
