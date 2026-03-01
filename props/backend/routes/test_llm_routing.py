"""Tests for LLM proxy upstream routing.

Tests that _get_upstream_route correctly resolves:
- OpenAI models (upstream_name=NULL) → default OpenAI upstream
- Custom models → configured upstreams with model name rewriting
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_bazel
from fastapi import HTTPException

from props.backend.routes.llm import _get_upstream_route, _resolve_upstream_url
from props.config import CustomModelConfig, PropsConfig, UpstreamConfig
from props.db.database import Database
from props.db.models import ModelMetadata


@pytest.fixture
def sample_config() -> PropsConfig:
    """Config with multiple upstreams."""
    return PropsConfig(
        backend_url="http://localhost:8000",
        agent_env={},
        upstreams={
            "local": UpstreamConfig(url="http://localhost:11434/v1", api_key_env="LOCAL_API_KEY"),
            "azure": UpstreamConfig(url_env="AZURE_OPENAI_URL", api_key_env="AZURE_API_KEY"),
        },
        models=[
            CustomModelConfig(
                name="local:llama-70b",
                upstream="local",
                upstream_model="llama3.3:70b",
                input_usd_per_1m_tokens=0.0,
                cached_input_usd_per_1m_tokens=0.0,
                output_usd_per_1m_tokens=0.0,
                context_window_tokens=128000,
                max_output_tokens=4096,
            )
        ],
    )


def test_resolve_upstream_url_static() -> None:
    """Static URL is returned directly."""
    config = UpstreamConfig(url="http://localhost:11434/v1", api_key_env="KEY")
    assert _resolve_upstream_url(config) == "http://localhost:11434/v1"


def test_resolve_upstream_url_from_env() -> None:
    """URL from env var is resolved."""
    config = UpstreamConfig(url_env="MY_URL_VAR", api_key_env="KEY")
    with patch.dict("os.environ", {"MY_URL_VAR": "http://test.example.com/v1"}):
        assert _resolve_upstream_url(config) == "http://test.example.com/v1"


def test_resolve_upstream_url_default_for_missing_env() -> None:
    """Missing env var falls back to OpenAI default."""
    config = UpstreamConfig(url_env="MISSING_VAR", api_key_env="KEY")
    with patch.dict("os.environ", {}, clear=True):
        assert _resolve_upstream_url(config) == "https://api.openai.com/v1"


def test_openai_model_uses_default_upstream(synced_db: Database, sample_config: PropsConfig) -> None:
    """OpenAI model (upstream_name=NULL) routes to default OpenAI upstream."""
    # Insert a model with no upstream info (simulating OpenAI model)
    with synced_db.session() as session:
        session.merge(
            ModelMetadata(
                model_id="gpt-4o",
                input_usd_per_1m_tokens=2.50,
                cached_input_usd_per_1m_tokens=1.25,
                output_usd_per_1m_tokens=10.00,
                context_window_tokens=128000,
                max_output_tokens=16384,
                upstream_name=None,
                upstream_model=None,
            )
        )
        session.commit()

    with synced_db.session() as session:
        with patch.dict("os.environ", {"OPENAI_BASE_URL": "https://api.openai.com/v1", "OPENAI_API_KEY": "sk-test"}):
            route = _get_upstream_route("gpt-4o", session, sample_config)
        assert route.url == "https://api.openai.com/v1"
        assert route.api_key == "sk-test"
        assert route.model_name == "gpt-4o"


def test_custom_model_routes_to_configured_upstream(synced_db: Database, sample_config: PropsConfig) -> None:
    """Custom model routes to its configured upstream with model rewriting."""
    # Insert a custom model pointing to "local" upstream
    with synced_db.session() as session:
        session.merge(
            ModelMetadata(
                model_id="local:llama-70b",
                input_usd_per_1m_tokens=0.0,
                cached_input_usd_per_1m_tokens=0.0,
                output_usd_per_1m_tokens=0.0,
                context_window_tokens=128000,
                max_output_tokens=4096,
                upstream_name="local",
                upstream_model="llama3.3:70b",
            )
        )
        session.commit()

    with synced_db.session() as session:
        with patch.dict("os.environ", {"LOCAL_API_KEY": "local-key"}):
            route = _get_upstream_route("local:llama-70b", session, sample_config)
        assert route.url == "http://localhost:11434/v1"
        assert route.api_key == "local-key"
        assert route.model_name == "llama3.3:70b"


def test_unknown_model_raises_400(synced_db: Database, sample_config: PropsConfig) -> None:
    """Unknown model ID raises HTTPException(400)."""
    with synced_db.session() as session:
        with pytest.raises(HTTPException) as exc_info:
            _get_upstream_route("nonexistent-model", session, sample_config)
        assert exc_info.value.status_code == 400
        assert "Unknown model" in exc_info.value.detail


def test_unknown_upstream_raises_500(synced_db: Database, sample_config: PropsConfig) -> None:
    """Model referencing unknown upstream raises HTTPException(500)."""
    # Insert a model pointing to nonexistent upstream
    with synced_db.session() as session:
        session.merge(
            ModelMetadata(
                model_id="broken-model",
                input_usd_per_1m_tokens=0.0,
                cached_input_usd_per_1m_tokens=0.0,
                output_usd_per_1m_tokens=0.0,
                context_window_tokens=8192,
                max_output_tokens=2048,
                upstream_name="nonexistent-upstream",
                upstream_model="model-name",
            )
        )
        session.commit()

    with synced_db.session() as session:
        with pytest.raises(HTTPException) as exc_info:
            _get_upstream_route("broken-model", session, sample_config)
        assert exc_info.value.status_code == 500
        assert "unknown upstream" in exc_info.value.detail


if __name__ == "__main__":
    pytest_bazel.main()
