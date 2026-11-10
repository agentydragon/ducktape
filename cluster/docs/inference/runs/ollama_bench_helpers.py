"""Pure prompt and output helpers shared by the April 28 Ollama runs."""

import json
import random
import statistics
from typing import Any

SEED = 42

WORDS = [
    "lorem",
    "ipsum",
    "dolor",
    "sit",
    "amet",
    "consectetur",
    "adipiscing",
    "elit",
    "sed",
    "do",
    "eiusmod",
    "tempor",
    "incididunt",
    "ut",
    "labore",
    "et",
    "dolore",
    "magna",
    "aliqua",
    "enim",
    "ad",
    "minim",
    "veniam",
    "quis",
    "nostrud",
    "exercitation",
    "ullamco",
    "laboris",
    "aliquip",
    "commodo",
    "consequat",
    "duis",
    "aute",
    "irure",
    "dolor",
    "reprehenderit",
    "voluptate",
    "velit",
    "esse",
    "cillum",
    "eu",
    "fugiat",
    "nulla",
    "pariatur",
    "excepteur",
    "sint",
    "occaecat",
    "cupidatat",
    "non",
    "proident",
    "sunt",
    "culpa",
    "qui",
    "officia",
    "deserunt",
    "mollit",
    "anim",
]


def make_prompt(target_tokens: int) -> str:
    """Generate a deterministic filler prompt with a long continuation target."""
    rng = random.Random(SEED)
    n_words = max(8, int(target_tokens * 1.4))
    body = " ".join(rng.choice(WORDS) for _ in range(n_words))
    return (
        "You will be given a passage of placeholder text. Your job is to "
        "continue it with at least 200 more words of similar nonsensical "
        "lorem-ipsum-style filler. Do not summarize, analyze, or comment "
        "on the passage. Just append more filler in the same style.\n\n"
        f"PASSAGE:\n{body}\n\nCONTINUATION (at least 200 more words):"
    )


def derive_tps(s: dict) -> dict:
    """Compute prefill / reasoning-decode / content-decode TPS.

    - prefill_tps: prompt_tokens / time-to-first-event (reasoning OR content)
    - reasoning_tps: reasoning_tokens / (t_first_content - t_first_reasoning),
      where reasoning_tokens is the server-reported count if available, else
      reasoning_chunks (lower bound).
    - content_tps: content_tokens / (t_done - t_first_content)
    """
    t_first = s["t_first_reasoning"] if s["t_first_reasoning"] is not None else s["t_first_content"]
    prefill = s["prompt_tokens"] / t_first if t_first and s["prompt_tokens"] else None

    reasoning_window = None
    if s["t_first_reasoning"] is not None and s["t_first_content"] is not None:
        reasoning_window = s["t_first_content"] - s["t_first_reasoning"]
    reasoning_tokens = s["reasoning_tokens_usage"] or s["reasoning_chunks"]
    reasoning_tps = reasoning_tokens / reasoning_window if reasoning_window and reasoning_tokens else None

    content_window = None
    content_tokens = s["completion_tokens"] - (s["reasoning_tokens_usage"] or 0)
    if s["t_first_content"] is not None:
        content_window = s["t_done"] - s["t_first_content"]
    content_tps = content_tokens / content_window if content_window and content_tokens else None

    return {
        **s,
        "prefill_tps": prefill,
        "reasoning_tps": reasoning_tps,
        "content_tps": content_tps,
        "content_tokens": content_tokens,
    }


def summarize(samples: list[dict]) -> dict[str, Any]:
    def col(key: str) -> list:
        return [s[key] for s in samples if s.get(key) is not None]

    out: dict[str, Any] = {"n": len(samples)}
    keys = (
        "t_first_reasoning",
        "t_first_content",
        "t_done",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens_usage",
        "content_tokens",
        "reasoning_chunks",
        "content_chunks",
        "prefill_tps",
        "reasoning_tps",
        "content_tps",
    )
    for k in keys:
        vals = col(k)
        if not vals:
            out[k] = None
            continue
        out[k] = {"p50": statistics.median(vals), "mean": statistics.mean(vals), "min": min(vals), "max": max(vals)}
    return out


def emit(event: str, **fields) -> None:
    """One JSONL line per event. Cheap to grep, cheap to parse."""
    print(json.dumps({"event": event, **fields}, default=str), flush=True)
