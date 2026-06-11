#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from hamcrest import any_of, assert_that, contains_string, equal_to, has_entries, has_item

from sysrw.run_eval import read_dataset
from sysrw.schemas import CCRSample, CrushSample


def text_block_contains(fragment: str):
    """Return a matcher that finds a text content block containing ``fragment``."""
    return has_item(has_entries(type="text", text=contains_string(fragment)))


@pytest.fixture
def ccr_min_path(test_data_dir: Path) -> Path:
    return test_data_dir / "ccr_min.jsonl"


@pytest.fixture
def crush_min_path(test_data_dir: Path) -> Path:
    return test_data_dir / "crush_min.jsonl"


async def test_read_ccr_min(ccr_min_path: Path):
    ds = await read_dataset(ccr_min_path)
    assert len(ds) == 2
    s = ds[0]
    assert s.correlation_id == "ccr-1"
    # Type narrowing: CCR dataset should only contain CCRSample instances
    assert isinstance(s, CCRSample), f"Expected CCRSample, got {type(s)}"
    msgs = s.anthropic_request.messages
    first_msg = msgs[0].model_dump()
    assert_that(first_msg["role"], equal_to("user"))
    # last user message should contain the <bad> marker
    last_user = next(m for m in reversed(msgs) if getattr(m, "role", None) == "user")
    last_user_dict = last_user.model_dump()
    # Matcher: has a content block that is text with substring
    assert_that(
        last_user_dict,
        has_entries(
            content=any_of(
                text_block_contains("<bad>"),
                # Some datasets encode content as a plain string
                contains_string("<bad>"),
            )
        ),
    )


async def test_read_crush_min(crush_min_path: Path):
    ds = await read_dataset(crush_min_path)
    assert len(ds) == 2
    s_bad = ds[0]
    # crush has no correlation_id semantics
    assert s_bad.correlation_id is None
    # Should be a CrushSample discriminated instance
    assert isinstance(s_bad, CrushSample)
    # Responses-native payload preserved as Pydantic model
    oai_req = s_bad.oai_request
    input_data = oai_req["input"]  # TypedDict access
    assert isinstance(input_data, list)
    # Extract roles from the input messages (TypedDict items remain as dicts)
    roles = [item["role"].lower() for item in input_data if isinstance(item, dict) and "role" in item]
    assert any(r in ("user", "assistant") for r in roles)


if __name__ == "__main__":
    pytest_bazel.main()
