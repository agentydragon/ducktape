#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Minimal inference-bench harness for a served OpenAI-compatible endpoint.

Measures the deployment-dependent things the PLAN says we always measure
locally: allocated/effective context, latency (TTFT + decode tok/s), and
tool-call round-trip reliability. Quality comes from external evals, not here.

Run against a port-forwarded vLLM service:

    kubectl -n llm-bench port-forward svc/vllm-qwen3-coder 8000:8000 &
    uv run --script bench.py --base-url http://localhost:8000/v1 \
        --model qwen3-coder-awq --out summary.json

The script is deliberately small and standalone (see PLAN.md "hobbyist scale").
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

# Rough English chars-per-token; only used to *build* prompts of a target size.
# Actual lengths always come back from the server's usage.prompt_tokens.
CHARS_PER_TOKEN = 3.8
NEEDLE = "The vault access code is ZQ7-4413-XK."
NEEDLE_ANSWER = "ZQ7-4413-XK"

# Extra request-body fields (e.g. disabling a reasoning model's thinking so the
# needle/tool probes get a direct answer in a normal budget). Set in main().
EXTRA_BODY: dict = {}


def build_filler(target_tokens: int) -> str:
    """A varied-but-cheap filler of about `target_tokens` tokens."""
    n_chars = int(target_tokens * CHARS_PER_TOKEN)
    parts, i = [], 0
    while sum(len(p) for p in parts) < n_chars:
        parts.append(
            f"Section {i}: the quick brown fox jumps over the lazy dog while "
            f"logging event number {i * 7 + 3} to the distributed ledger. "
        )
        i += 1
    return "".join(parts)


@dataclass
class RequestResult:
    ok: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # ttft_s clocks the first token of ANY kind (reasoning or content), so it is
    # honest for reasoning models whose reasoning tokens precede the answer.
    ttft_s: float | None = None
    # ttfc_s = time to first *content* (answer) token; for a reasoning model the
    # gap ttfc_s - ttft_s is the reasoning latency before the user sees an answer.
    ttfc_s: float | None = None
    total_s: float | None = None
    # decode_tps counts ALL output tokens (reasoning + content) over generation
    # time — the apples-to-apples tokens/s: what you pay in tokens is capability.
    decode_tps: float | None = None
    reasoning_tokens: int | None = None
    error: str | None = None
    text: str | None = None
    reasoning_text: str | None = None


def stream_chat(base_url: str, model: str, messages: list[dict], max_tokens: int, timeout: float) -> RequestResult:
    """One streaming chat completion; times TTFT and decode throughput."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        **EXTRA_BODY,
    }
    dispatch = time.monotonic()
    first_tok: float | None = None
    first_content: float | None = None
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    usage: dict | None = None
    try:
        with httpx.stream("POST", f"{base_url}/chat/completions", json=body, timeout=timeout) as r:
            if r.status_code != 200:
                return RequestResult(ok=False, error=f"HTTP {r.status_code}: {r.read()[:300]!r}")
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload.strip() == "[DONE]":
                    break
                event = json.loads(payload)
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    # vLLM reasoning parsers stream the CoT under either
                    # `reasoning_content` (deepseek_r1 etc.) or `reasoning` (qwen3).
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    content = delta.get("content")
                    if (reasoning or content) and first_tok is None:
                        first_tok = time.monotonic()
                    if reasoning:
                        reasoning_chunks.append(reasoning)
                    if content:
                        if first_content is None:
                            first_content = time.monotonic()
                        chunks.append(content)
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        return RequestResult(ok=False, error=f"{type(e).__name__}: {e}")

    end = time.monotonic()
    if usage is None or first_tok is None:
        return RequestResult(ok=False, error="no usage/first-token in stream", text="".join(chunks))
    completion = usage["completion_tokens"]
    decode_tps = completion / (end - first_tok) if end > first_tok and completion else None
    return RequestResult(
        ok=True,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=completion,
        ttft_s=first_tok - dispatch,
        ttfc_s=(first_content - dispatch) if first_content is not None else None,
        total_s=end - dispatch,
        decode_tps=decode_tps,
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        text="".join(chunks),
        reasoning_text="".join(reasoning_chunks),
    )


@dataclass
class LatencyPoint:
    target_ctx: int
    prompt_tokens: int | None
    ttft_p50: float | None
    ttft_p95: float | None
    ttfc_p50: float | None  # time to first content token (answer, post-reasoning)
    decode_tps_median: float | None  # all output tokens (incl. reasoning) / gen time
    reasoning_tokens_median: float | None
    reps_ok: int
    reps: int


def measure_latency(base_url, model, target_ctx: int, reps: int, timeout: float) -> LatencyPoint:
    prompt = build_filler(target_ctx) + "\n\nReply with a one-sentence summary."
    results = [stream_chat(base_url, model, [{"role": "user", "content": prompt}], 256, timeout) for _ in range(reps)]
    ok = [r for r in results if r.ok]
    ttfts = sorted(r.ttft_s for r in ok if r.ttft_s is not None)
    ttfcs = sorted(r.ttfc_s for r in ok if r.ttfc_s is not None)

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None

    tps = [r.decode_tps for r in ok if r.decode_tps]
    rtoks = [r.reasoning_tokens for r in ok if r.reasoning_tokens is not None]
    return LatencyPoint(
        target_ctx=target_ctx,
        prompt_tokens=ok[0].prompt_tokens if ok else None,
        ttft_p50=pct(ttfts, 0.5),
        ttft_p95=pct(ttfts, 0.95),
        ttfc_p50=pct(ttfcs, 0.5),
        decode_tps_median=statistics.median(tps) if tps else None,
        reasoning_tokens_median=statistics.median(rtoks) if rtoks else None,
        reps_ok=len(ok),
        reps=reps,
    )


@dataclass
class ContextRung:
    target_ctx: int
    allocated_ok: bool
    prompt_tokens: int | None = None
    error: str | None = None
    needle_depths_ok: list[float] = field(default_factory=list)
    needle_depths_fail: list[float] = field(default_factory=list)


def measure_context(
    base_url, model, target_ctx: int, depths: list[float], timeout: float, needle_max_tokens: int = 512
) -> ContextRung:
    """Allocated check (does a ~target_ctx request complete) + needle probe.

    needle_max_tokens must be generous: a verbose reasoning model spends most of
    its budget on chain-of-thought before emitting the answer (E4 lesson), so a
    tight budget truncates the answer and falsely reports a needle miss.
    """
    rung = ContextRung(target_ctx=target_ctx, allocated_ok=False)
    for depth in depths:
        body_tokens = target_ctx - 4096
        filler = build_filler(body_tokens)
        cut = int(len(filler) * depth)
        doc = filler[:cut] + " " + NEEDLE + " " + filler[cut:]
        messages = [
            {
                "role": "user",
                "content": doc + "\n\nWhat is the vault access code mentioned in the text above? "
                "Reply with only the code.",
            }
        ]
        res = stream_chat(base_url, model, messages, needle_max_tokens, timeout)
        if not res.ok:
            rung.error = res.error
            return rung
        rung.allocated_ok = True
        rung.prompt_tokens = res.prompt_tokens
        # Retrieval hit only if the code is in the model's FINAL ANSWER (content),
        # not its reasoning. A reasoning model must have a budget large enough to
        # finish thinking AND answer; if it can't, that's a real limitation, not
        # something to paper over by scanning the chain-of-thought.
        if NEEDLE_ANSWER in (res.text or ""):
            rung.needle_depths_ok.append(depth)
        else:
            rung.needle_depths_fail.append(depth)
    return rung


def tool_smoke(base_url, model, timeout: float) -> dict:
    """Single, parallel, and multi-turn tool-call round trips with fixed schemas."""
    weather = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }

    def call(messages, tools, tool_choice="auto"):
        body = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": 0.0,
            "max_tokens": 1024,  # generous: reasoning models emit CoT before the tool call
            **EXTRA_BODY,
        }
        r = httpx.post(f"{base_url}/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    out: dict = {}
    # 1. single tool call
    try:
        m = call([{"role": "user", "content": "What's the weather in Paris?"}], [weather])
        calls = m.get("tool_calls") or []
        args = json.loads(calls[0]["function"]["arguments"]) if calls else {}
        out["single"] = {"ok": len(calls) == 1 and args.get("city", "").lower() == "paris", "n_calls": len(calls)}
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
        out["single"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 2. parallel tool calls
    try:
        m = call(
            [{"role": "user", "content": "Compare the weather in Paris and Tokyo. Call the tool for each city."}],
            [weather],
        )
        calls = m.get("tool_calls") or []
        cities = {json.loads(c["function"]["arguments"]).get("city", "").lower() for c in calls}
        out["parallel"] = {"ok": {"paris", "tokyo"} <= cities, "n_calls": len(calls), "cities": sorted(cities)}
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
        out["parallel"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 3. multi-turn: model asks tool, we answer, model uses result
    try:
        msgs: list[dict] = [
            {"role": "user", "content": "What's the weather in Berlin? Use the tool, then tell me in one sentence."}
        ]
        m = call(msgs, [weather])
        calls = m.get("tool_calls") or []
        if calls:
            msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
            msgs.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": "18C and sunny"})
            final = call(msgs, [weather], tool_choice="none")
            txt = (final.get("content") or "").lower()
            out["multi_turn"] = {"ok": "18" in txt or "sunny" in txt, "reply": final.get("content")}
        else:
            out["multi_turn"] = {"ok": False, "error": "no tool call on first turn"}
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
        out["multi_turn"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="e.g. http://localhost:8000/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="summary.json")
    ap.add_argument("--latency-ctx", type=int, nargs="*", default=[8192, 32768, 131072])
    ap.add_argument("--context-rungs", type=int, nargs="*", default=[131072, 262144])
    ap.add_argument(
        "--needle-max-tokens", type=int, default=512, help="output budget for needle probe; raise for verbose reasoners"
    )
    ap.add_argument(
        "--no-think",
        action="store_true",
        help="disable a Qwen reasoning model's thinking (enable_thinking=false) for direct answers",
    )
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--skip-context", action="store_true")
    args = ap.parse_args()

    summary: dict = {"model": args.model, "base_url": args.base_url}

    print("== tool-call smoke ==", file=sys.stderr)
    summary["tool_calls"] = tool_smoke(args.base_url, args.model, args.timeout)
    print(json.dumps(summary["tool_calls"], indent=2), file=sys.stderr)

    print("== latency ==", file=sys.stderr)
    summary["latency"] = []
    for ctx in args.latency_ctx:
        pt = measure_latency(args.base_url, args.model, ctx, args.reps, args.timeout)
        summary["latency"].append(asdict(pt))
        print(
            f"  ctx~{ctx}: ptok={pt.prompt_tokens} ttft_p50={pt.ttft_p50} "
            f"ttfc_p50={pt.ttfc_p50} decode_tps={pt.decode_tps_median} "
            f"reasoning_tok={pt.reasoning_tokens_median} ({pt.reps_ok}/{pt.reps})",
            file=sys.stderr,
        )

    if not args.skip_context:
        print("== context ladder ==", file=sys.stderr)
        summary["context"] = []
        for ctx in args.context_rungs:
            rung = measure_context(
                args.base_url, args.model, ctx, [0.1, 0.5, 0.9], args.timeout, args.needle_max_tokens
            )
            summary["context"].append(asdict(rung))
            print(
                f"  ctx~{ctx}: allocated={rung.allocated_ok} ptok={rung.prompt_tokens} "
                f"needle_ok={rung.needle_depths_ok} fail={rung.needle_depths_fail} "
                f"err={rung.error}",
                file=sys.stderr,
            )
            if not rung.allocated_ok:
                break

    with Path(args.out).open("w") as f:
        json.dump(summary, f, indent=2)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
