import os

import openai
import pytest


@pytest.mark.live_llm
async def test_responses_nonstreaming_live(tmp_path):
    """Live test: call OpenAI Responses.create (non-streaming).

    Requires OPENAI_API_KEY in the environment. Uses OPENAI_MODEL if set or
    falls back to 'o4-mini'. This test is explicitly marked `live_llm` and is
    excluded by default in CI runs.
    """
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; skipping live test")

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    # Minimal input that should be supported by Responses API
    inp = [{"role": "user", "content": "Say hello in one short sentence."}]

    # Non-streaming call
    resp = await client.responses.create(model=model, input=inp)

    # Try to normalize to dict for assertions
    try:
        data = resp.model_dump(exclude_none=True)
    except Exception:
        # If model_dump not present, assume dict-like
        data = resp if isinstance(resp, dict) else None

    assert data is not None, "Response payload missing"
    # Expect an 'id' or 'object' token from Responses API
    assert ("id" in data) or (data.get("object") is not None)


@pytest.mark.live_llm
async def test_responses_streaming_live(tmp_path):
    """Live test: call OpenAI Responses.create with stream=True and iterate.

    Requires OPENAI_API_KEY in the environment. The test collects streamed
    events and asserts that at least one event was received.
    """
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; skipping live test")

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    inp = [{"role": "user", "content": "Stream: say numbers 1..3 as separate events"}]

    # The SDK may return a coroutine that yields an async iterator; await it first
    maybe_iter = await client.responses.create(model=model, input=inp, stream=True)

    # Support both async iterables and sync iterables returned by the SDK wrapper
    got_any = False
    items = []
    if hasattr(maybe_iter, "__aiter__"):
        async for event in maybe_iter:
            got_any = True
            try:
                items.append(event.model_dump(exclude_none=True))
            except Exception:
                items.append(event if isinstance(event, dict) else None)
    else:
        for event in maybe_iter:
            got_any = True
            try:
                items.append(event.model_dump(exclude_none=True))
            except Exception:
                items.append(event if isinstance(event, dict) else None)

    assert got_any, "No stream events received"
    assert any(it is not None for it in items), "Stream events contained no usable payload"
