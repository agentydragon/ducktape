"""History Service fixtures for the fold's database-backed tests."""

from collections.abc import AsyncIterator

import pytest

from agentplane.history_service.testing.fixtures import (
    history_service,
    history_writer_namespaces,
    service_database_template,
    service_history_db_url,
)
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.history_service.testing.history import History
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
async def history(history_service: GoHistoryService) -> AsyncIterator[History]:
    async with History(history_service) as scripted:
        yield scripted
