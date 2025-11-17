#!/usr/bin/env python3
from __future__ import annotations

from importlib import resources
from pathlib import Path

from hamcrest import any_of, assert_that, contains_string, equal_to, has_entries, has_item
from openai.types.responses import ResponseInputMessageItem
import pytest

from adgn.llm.sysrw.run_eval import read_dataset  # type: ignore
from adgn.llm.sysrw.schemas import CCRSample, CrushSample

ROOT = Path(str(resources.files("adgn.llm.sysrw")))
DATA = ROOT / "data" / "_test"

# TODO: Add test datasets to repo
# Currently these tests are skipped because test datasets (ccr_min.jsonl, crush_min.jsonl)
# are not checked into the repository. Need to add minimal test datasets to
# adgn/src/adgn/llm/sysrw/data/_test/ directory and ensure they're included in package.


def text_block_contains(fragment: str):
    """Return a matcher that finds a text content block containing ``fragment``."""

    return has_item(has_entries(type="text", text=contains_string(fragment)))


@pytest.mark.skipif(not (DATA / "ccr_min.jsonl").exists(), reason="ccr_min.jsonl test dataset missing")
async def test_read_ccr_min():
    ds = await read_dataset(DATA / "ccr_min.jsonl")
    assert len(ds) == 2
    s = ds[0]
    assert s.correlation_id == "ccr-1"
    # Type narrowing: CCR dataset should only contain CCRSample instances
    assert isinstance(s, CCRSample), f"Expected CCRSample, got {type(s)}"
    msgs = s.anthropic_request.messages
    assert_that(msgs[0]["role"], equal_to("user"))
    # last user message should contain the <bad> marker
    last_user = next(m for m in reversed(msgs) if m.get("role") == "user")
    # Matcher: has a content block that is text with substring
    assert_that(
        last_user,
        has_entries(
            content=any_of(
                text_block_contains("<bad>"),
                # Some datasets encode content as a plain string
                contains_string("<bad>"),
            )
        ),
    )


@pytest.mark.skipif(not (DATA / "crush_min.jsonl").exists(), reason="crush_min.jsonl test dataset missing")
async def test_read_crush_min():
    ds = await read_dataset(DATA / "crush_min.jsonl")
    assert len(ds) == 2
    s_bad = ds[0]
    # crush has no correlation_id semantics
    assert s_bad.correlation_id is None
    # Should be a CrushSample discriminated instance
    assert isinstance(s_bad, CrushSample)
    # Responses-native payload preserved as Pydantic model
    oai_req = s_bad.oai_request
    input_data = oai_req.input
    assert isinstance(input_data, list)
    # Extract roles from the input messages (should be proper message objects)
    roles = [item.role.lower() for item in input_data if isinstance(item, ResponseInputMessageItem)]
    assert any(r in ("user", "assistant") for r in roles)
