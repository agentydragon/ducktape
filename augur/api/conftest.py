from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from augur.api.config import Config
from augur.api.server import ApiServerConfig, create_app
from augur.api.testing import load_fixture_config
from augur.product.testing import capacity_limited_private_equity_fixture, forced_private_equity_event_fixture


@pytest.fixture(scope="module")
def augur_config() -> Config:
    return load_fixture_config()


@pytest.fixture
def forced_private_equity_event_client(augur_config: Config) -> Iterator[TestClient]:
    with _client_with(augur_config, {"current_model": forced_private_equity_event_fixture()}) as client:
        yield client


@pytest.fixture
def capacity_limited_private_equity_client(augur_config: Config) -> Iterator[TestClient]:
    with _client_with(augur_config, {"current_model": capacity_limited_private_equity_fixture()}) as client:
        yield client


def _client_with(augur_config: Config, models: dict[str, Any]) -> TestClient:
    return TestClient(create_app(ApiServerConfig(augur_config=augur_config, models=models, price_clients={})))
