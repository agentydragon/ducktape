from __future__ import annotations

from datetime import UTC, datetime

import pytest_bazel

from loom.gym.agent_eval import _default_eval_session_id, _litellm_metadata, _parse_csv


def test_parse_csv_trims_empty_values() -> None:
    assert _parse_csv("loom-gym, wayback-archive,,manifold ") == ["loom-gym", "wayback-archive", "manifold"]


def test_default_eval_session_id_uses_utc_timestamp() -> None:
    assert _default_eval_session_id(datetime(2026, 6, 12, 5, 25, 14, tzinfo=UTC)) == "loom-gym-20260612T052514Z"


def test_litellm_metadata_carries_langfuse_and_eval_fields() -> None:
    metadata = _litellm_metadata(
        session_id="loom-gym-test",
        tags=["loom-gym", "wayback-archive"],
        model_id="glm-4.5",
        endpoint_model="glm-4.5-anthropic",
        task_filter="manifold-",
        archive=True,
        wayback_upstream="http://wayback-cache",
    )

    assert metadata == {
        "trace_user_id": "loom-gym",
        "session_id": "loom-gym-test",
        "tags": ["loom-gym", "wayback-archive"],
        "loom.eval.session_id": "loom-gym-test",
        "loom.eval.model_id": "glm-4.5",
        "loom.eval.endpoint_model": "glm-4.5-anthropic",
        "loom.eval.task_filter": "manifold-",
        "loom.eval.archive": True,
        "loom.eval.wayback_upstream": "http://wayback-cache",
    }


if __name__ == "__main__":
    pytest_bazel.main()
