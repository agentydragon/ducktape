"""Throughput smoke benchmark against an OpenAI-compatible chat endpoint.

Reasoning-aware: tracks `delta.reasoning_content` chunks separately from
`delta.content` and reports prefill / reasoning-decode / content-decode
phases. Sets `reasoning_effort: low` and Ollama's `think: false` (via
`extra_body`-style top-level field) to suppress reasoning when measuring
raw throughput; for reasoning-quality work, run a separate sweep with
high effort and a problem-solving prompt.

Per (model, input_len): one warmup request (forces model load on Ollama,
captures cold-load time), then N measurement requests. Outputs at
~200 tokens of content so the decode window is long enough to be
measurable above the streaming-buffer noise floor.

Final summary printed as `RESULT_JSON:{...}` for easy log scraping.
"""

import json
import os
import random
import statistics
import sys
import time
import urllib.request
from typing import Any

# Bypass injected HTTP_PROXY env so requests reach `ollama.ollama:11434`
# directly. urllib's NO_PROXY matching is hostname-based and doesn't resolve
# the cluster Service hostname against the 10.0.0.0/8 CIDR in the injected
# NO_PROXY list.
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE_URL = os.environ.get("OLLAMA_URL", "http://ollama.ollama:11434/v1")
MODELS = [
    m.strip() for m in os.environ.get("MODELS", "gpt-oss:20b,gpt-oss:120b,gemma4:31b-it-q8_0").split(",") if m.strip()
]
INPUT_LENS = [int(x) for x in os.environ.get("INPUT_LENS", "1024,8192").split(",")]
OUTPUT_LEN = int(os.environ.get("OUTPUT_LEN", "256"))
N_TRIALS = int(os.environ.get("N_TRIALS", "5"))
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "off")  # "off" | "low" | "medium" | "high"
WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "1800"))
TRIAL_TIMEOUT = float(os.environ.get("TRIAL_TIMEOUT", "600"))
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
    """Generate a prompt that forces ~200 tokens of continuation.

    Asking for "the single word OK" (v1) made the model stop in <50 tokens,
    which collapses the decode window below streaming-buffer granularity
    and makes decode-TPS measurements meaningless. Asking for a story
    risks triggering reasoning. So we ask the model to **continue**
    nonsensical filler with more nonsensical filler — a task that is
    neither short nor reasoning-worthy.
    """
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


def stream_request(model: str, prompt: str, timeout: float) -> dict:
    req_body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": OUTPUT_LEN,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if REASONING_EFFORT == "off":
        # Ollama-native: `think: false` disables reasoning for gemma3/4 and
        # similar models that reason by default. The OpenAI-spec
        # `reasoning_effort` field has no documented "off" value, so we omit
        # it and rely on `think: false`.
        req_body["think"] = False
    else:
        # gpt-oss honors both fields; gemma seems to ignore effort levels and
        # only respect the boolean. Send both for portability.
        req_body["reasoning_effort"] = REASONING_EFFORT
        req_body["think"] = REASONING_EFFORT
    body = json.dumps(req_body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    t_first_reasoning: float | None = None
    t_first_content: float | None = None
    prompt_tokens = 0
    completion_tokens = 0  # what server reports in usage.completion_tokens
    reasoning_tokens_usage = 0  # from usage.completion_tokens_details if present
    reasoning_chunks = 0
    content_chunks = 0
    with _no_proxy_opener.open(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                # Ollama uses `delta.reasoning_content` for gpt-oss / DeepSeek-style
                # models and `delta.reasoning` for gemma3/4. Either signals a
                # reasoning chunk; treat them identically.
                if delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking"):
                    if t_first_reasoning is None:
                        t_first_reasoning = time.monotonic() - t0
                    reasoning_chunks += 1
                if delta.get("content"):
                    if t_first_content is None:
                        t_first_content = time.monotonic() - t0
                    content_chunks += 1
            usage = chunk.get("usage")
            if usage:
                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                completion_tokens = usage.get("completion_tokens", completion_tokens)
                details = usage.get("completion_tokens_details") or {}
                reasoning_tokens_usage = details.get("reasoning_tokens", reasoning_tokens_usage) or 0
    t_done = time.monotonic() - t0
    return {
        "t_first_reasoning": t_first_reasoning,
        "t_first_content": t_first_content,
        "t_done": t_done,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens_usage": reasoning_tokens_usage,
        "reasoning_chunks": reasoning_chunks,
        "content_chunks": content_chunks,
    }


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


def main():
    summary: dict[str, Any] = {
        "base_url": BASE_URL,
        "output_len": OUTPUT_LEN,
        "n_trials": N_TRIALS,
        "reasoning_effort": REASONING_EFFORT,
        "configs": {},
    }
    for model in MODELS:
        per_model: dict[str, Any] = {"input_lens": {}}

        emit("warmup_start", model=model, timeout=WARMUP_TIMEOUT)
        try:
            warm = derive_tps(stream_request(model, make_prompt(64), timeout=WARMUP_TIMEOUT))
            per_model["warmup"] = warm
            emit("warmup", model=model, **warm)
        except Exception as e:
            emit("warmup_error", model=model, error=str(e))
            per_model["error"] = str(e)
            summary["configs"][model] = per_model
            continue

        for ilen in INPUT_LENS:
            prompt = make_prompt(ilen)
            samples = []
            for i in range(N_TRIALS):
                try:
                    s = derive_tps(stream_request(model, prompt, timeout=TRIAL_TIMEOUT))
                    samples.append(s)
                    emit("trial", model=model, input_len=ilen, i=i + 1, n=N_TRIALS, **s)
                except Exception as e:
                    emit("trial_error", model=model, input_len=ilen, i=i + 1, n=N_TRIALS, error=str(e))
            per_model["input_lens"][str(ilen)] = {"samples": samples, "summary": summarize(samples)}

        summary["configs"][model] = per_model

    print("RESULT_JSON:" + json.dumps(summary), flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
