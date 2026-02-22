"""Benchmark Ollama models via the LiteLLM OpenAI-compatible proxy.

Measures text generation (tg) and prompt processing (pp) throughput at
multiple context lengths. Each configuration runs for up to TIME_LIMIT_S
seconds (default 60s) or until MIN_SAMPLES (default 10) are collected,
whichever comes first.

Usage:
    bazel run //experimental/benchmark_ollama:benchmark_ollama
    bazel run //experimental/benchmark_ollama:benchmark_ollama -- --models gpt-oss-20b-128k

Environment:
    OLLAMA_API_KEY  API key for ollama.allegedly.works (optional)

Methodology:
    tg rate: short seed prompt + max_tokens=128, stream=True.
             Rate = (tokens_generated - 1) / (last_chunk_ts - first_chunk_ts)
             Averaged over repeated calls within the time budget.

    pp rate: prompt of ~N tokens + max_tokens=1, non-streaming.
             Rate = prompt_tokens / wall_clock_time (includes network RTT).
             Averaged over repeated calls within the time budget.

    Long-context: num_ctx is passed per-request via extra_body so Ollama
    dynamically sizes the KV cache. This allows testing beyond the server
    default (131072) up to VRAM limits.
"""

import argparse
import os
import statistics
import time

import openai

OLLAMA_BASE_URL = "https://ollama.allegedly.works/v1"
DEFAULT_MODELS = ["gpt-oss-20b-128k", "gpt-oss-120b-128k"]

TIME_LIMIT_S = 60  # max seconds per benchmark config
MIN_SAMPLES = 10  # stop early once this many samples are collected

# Filler used to build prompts of a target token count.
# "token " ≈ 1.5 tokens in most tokenisers; we repeat a longer phrase.
_FILLER_PHRASE = "The quick brown fox jumps over the lazy dog near the riverbank. "
# Approximate chars-per-token for English wordpiece tokenisers.
_CHARS_PER_TOKEN = 4.5

# Context lengths (in tokens) to benchmark prompt processing at.
# The server default is 1M (OLLAMA_NUM_CTX=1048576). Per-request num_ctx
# overrides are sent via extra_body so larger sizes are attempted automatically;
# the run stops at the first failure (typically VRAM exhaustion).
PP_CONTEXT_SIZES = [1_000, 4_000, 16_000, 32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000]

# Generation benchmark uses a short seed prompt; output length is fixed.
TG_MAX_TOKENS = 128
TG_SEED_PROMPT = "Write a detailed essay about the history of computing:"


def make_client() -> openai.OpenAI:
    api_key = os.environ.get("OLLAMA_API_KEY") or "no-key"
    return openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key=api_key)


def make_prompt(target_tokens: int) -> str:
    """Build a filler prompt of approximately target_tokens tokens."""
    target_chars = int(target_tokens * _CHARS_PER_TOKEN)
    full_repeats = target_chars // len(_FILLER_PHRASE)
    remainder = target_chars % len(_FILLER_PHRASE)
    return _FILLER_PHRASE * full_repeats + _FILLER_PHRASE[:remainder]


def check_model_available(client: openai.OpenAI, model: str) -> bool:
    models = {m.id for m in client.models.list()}
    return model in models


def run_tg(client: openai.OpenAI, model: str, time_limit: float) -> list[float]:
    """Run tg benchmarks until time_limit elapsed or MIN_SAMPLES gathered."""
    results: list[float] = []
    deadline = time.monotonic() + time_limit

    while time.monotonic() < deadline and len(results) < MIN_SAMPLES:
        first_ts: float | None = None
        last_ts: float | None = None
        token_count = 0

        stream = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": TG_SEED_PROMPT}], max_tokens=TG_MAX_TOKENS, stream=True
        )
        for chunk in stream:
            if time.monotonic() > deadline:
                break
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                now = time.monotonic()
                if first_ts is None:
                    first_ts = now
                last_ts = now
                token_count += 1

        if first_ts and last_ts and last_ts > first_ts and token_count > 1:
            results.append((token_count - 1) / (last_ts - first_ts))

    return results


def run_pp(client: openai.OpenAI, model: str, target_tokens: int, time_limit: float) -> list[float]:
    """Run pp benchmarks until time_limit elapsed or MIN_SAMPLES gathered."""
    prompt = make_prompt(target_tokens)
    results: list[float] = []
    deadline = time.monotonic() + time_limit

    while time.monotonic() < deadline and len(results) < MIN_SAMPLES:
        start = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1,
                stream=False,
                extra_body={"options": {"num_ctx": int(target_tokens * 1.1 + 512)}},
            )
        except openai.APIError as exc:
            print(f" [error: {exc}]", flush=True)
            break
        elapsed = time.monotonic() - start
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
        if prompt_tokens and elapsed > 0:
            results.append(prompt_tokens / elapsed)

    return results


def fmt(values: list[float]) -> str:
    if not values:
        return "N/A"
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    n = len(values)
    return f"{mean:.1f} ± {stdev:.1f} (n={n})"


def fmt_short(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{statistics.mean(values):.1f} t/s"


def benchmark_model(
    client: openai.OpenAI, model: str, pp_sizes: list[int], time_limit: float
) -> dict[str, list[float]]:
    print(f"\n{'=' * 60}", flush=True)
    print(f"Model: {model}", flush=True)
    print(f"{'=' * 60}", flush=True)

    if not check_model_available(client, model):
        print("  SKIP: model not available (not yet pulled?)")
        return {}

    results: dict[str, list[float]] = {}

    print(f"  tg{TG_MAX_TOKENS} (up to {time_limit:.0f}s)...", end=" ", flush=True)
    tg = run_tg(client, model, time_limit)
    results[f"tg{TG_MAX_TOKENS}"] = tg
    print(fmt(tg), flush=True)

    for ctx in pp_sizes:
        label = f"pp{ctx // 1000}k"
        print(f"  {label} (up to {time_limit:.0f}s)...", end=" ", flush=True)
        pp = run_pp(client, model, ctx, time_limit)
        results[label] = pp
        print(fmt(pp), flush=True)
        if not pp:
            print(f"    (stopped — likely hit VRAM limit at {ctx:,} tokens)", flush=True)
            break

    return results


def print_summary(all_results: dict[str, dict[str, list[float]]], models: list[str], pp_sizes: list[int]) -> None:
    metrics = [f"tg{TG_MAX_TOKENS}"] + [f"pp{s // 1000}k" for s in pp_sizes]
    col = 28

    print(f"\n{'=' * 70}")
    print("SUMMARY — mean ± stdev (t/s)")
    print(f"{'=' * 70}")
    header = f"{'Metric':<12}" + "".join(f"{m:<{col}}" for m in models)
    print(header)
    print("-" * len(header))
    for metric in metrics:
        row = f"{metric:<12}"
        for m in models:
            vals = all_results.get(m, {}).get(metric, [])
            row += f"{fmt_short(vals):<{col}}"
        print(row)
    print()
    print("Notes:")
    print("  tg = text generation, measured between first and last streaming chunk")
    print("  pp = prompt processing, wall-clock time (includes network RTT)")
    print(f"  Each config ran for up to {TIME_LIMIT_S}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Ollama models via LiteLLM proxy")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument(
        "--pp-sizes", nargs="+", type=int, default=PP_CONTEXT_SIZES, help="Prompt sizes in tokens to benchmark pp at"
    )
    parser.add_argument("--time-limit", type=float, default=TIME_LIMIT_S, help="Seconds per config")
    args = parser.parse_args()

    client = make_client()

    print(f"Ollama benchmark — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Endpoint: {OLLAMA_BASE_URL}")
    print(f"Models:   {', '.join(args.models)}")
    print(f"PP sizes: {args.pp_sizes}")
    print(f"Time limit per config: {args.time_limit:.0f}s")

    all_results: dict[str, dict[str, list[float]]] = {}
    for model in args.models:
        all_results[model] = benchmark_model(client, model, args.pp_sizes, args.time_limit)

    print_summary(all_results, args.models, args.pp_sizes)


if __name__ == "__main__":
    main()
