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
import sys
import time
import urllib.request
from typing import Any

from ollama_bench_helpers import derive_tps, emit, make_prompt, summarize

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
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low")
WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "1800"))
TRIAL_TIMEOUT = float(os.environ.get("TRIAL_TIMEOUT", "600"))


def stream_request(model: str, prompt: str, timeout: float) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": OUTPUT_LEN,
            "stream": True,
            "stream_options": {"include_usage": True},
            # OpenAI-spec field; Ollama maps this to its `think` knob for gpt-oss.
            "reasoning_effort": REASONING_EFFORT,
            # Ollama-native field: also pass explicitly in case the OpenAI
            # field isn't honored.
            "think": REASONING_EFFORT if REASONING_EFFORT != "off" else False,
        }
    ).encode()
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
                if delta.get("reasoning_content"):
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
