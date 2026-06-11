"""Tests for sync_model_metadata_with_session.

Verifies that the sync correctly propagates content changes (not just additions/
deletions) to existing rows. The previous count-based fast path skipped updates
when the model count hadn't changed, causing stale upstream_model values in the DB.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
import pytest_bazel
from sqlalchemy.orm import Session

from props.config import CustomModelConfig, PropsConfig, UpstreamConfig
from props.db.database import Database
from props.db.models import ModelMetadata
from props.db.sync.model_metadata import sync_model_metadata_with_session


def _make_config(upstream_model: str) -> PropsConfig:
    return PropsConfig(
        backend_url="http://props:8000",
        agent_env={},
        upstreams={"ollama": UpstreamConfig(url="http://ollama:11434/v1", api_key_env="OLLAMA_API_KEY")},
        models=[
            CustomModelConfig(
                name="gpt-oss-20b-128k",
                upstream="ollama",
                upstream_model=upstream_model,
                input_usd_per_1m_tokens=0,
                cached_input_usd_per_1m_tokens=0,
                output_usd_per_1m_tokens=0,
                context_window_tokens=131072,
                max_output_tokens=131072,
            )
        ],
    )


@pytest.fixture
def session(db: Database) -> Generator[Session]:
    with db.session() as s:
        yield s
        s.rollback()


def test_sync_updates_upstream_model_when_changed(session: Session) -> None:
    """Sync must update upstream_model even when the model count hasn't changed.

    Regression test: the old count-based fast path would skip updates when count
    matched, leaving stale upstream_model in DB after a config change.
    """
    # Populate DB with the model using the old upstream_model name
    sync_model_metadata_with_session(session, _make_config("gpt-oss:20b-old"))
    session.flush()

    old_row = session.get(ModelMetadata, "gpt-oss-20b-128k")
    assert old_row is not None
    assert old_row.upstream_model == "gpt-oss:20b-old"

    # Re-sync with the corrected upstream_model — count is the same
    sync_model_metadata_with_session(session, _make_config("gpt-oss:20b"))
    session.flush()

    session.expire(old_row)
    updated_row = session.get(ModelMetadata, "gpt-oss-20b-128k")
    assert updated_row is not None
    assert updated_row.upstream_model == "gpt-oss:20b", (
        "Sync must update upstream_model even when model count hasn't changed"
    )


if __name__ == "__main__":
    pytest_bazel.main()
