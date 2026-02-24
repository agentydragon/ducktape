"""Benchmark Ollama models via the LiteLLM OpenAI-compatible proxy.

Measures output speed, input speed at multiple context lengths, and
needle-in-haystack recall (niah). Each configuration runs for up to
--time-limit seconds (default 60s) or --min-samples samples, whichever comes
first.

Usage:
    bazel run //hack/benchmark_ollama:benchmark_ollama
    bazel run //hack/benchmark_ollama:benchmark_ollama -- --models gpt-oss-20b-128k
    bazel run //hack/benchmark_ollama:benchmark_ollama -- --niah-samples 0  # skip NIAH
    bazel run //hack/benchmark_ollama:benchmark_ollama -- --unload-between-models

Environment:
    OLLAMA_API_KEY  API key for ollama.allegedly.works (optional)

Methodology:
    prewarm: one throwaway request per context size to allocate KV cache.

    output:  short seed prompt + max_tokens=128, stream=True.
             Rate = (tokens_generated - 1) / (last_chunk_ts - first_chunk_ts).
             Measures decode (output token generation) speed at minimal context.

    decode:  filler prompt of ~N tokens + max_tokens=128, stream=True.
             Same rate calculation as output, but after prefilling a large context.
             Measures how decode speed degrades with KV cache size.

    input:   filler prompt of ~N tokens + max_tokens=1, non-streaming.
             Rate = prompt_tokens / wall_clock_time (includes network RTT).
             Measures prefill (input token processing) speed.

    niah:    haystack (War and Peace) with an embedded 8-char hex needle at
             evenly spaced depths (0.0 to 1.0), streamed response (reasoning
             model needs streaming to capture thinking tokens). Scored by
             needle presence in full stream. Samples run until time limit or
             --niah-samples collected.

    num_ctx is passed per-request via extra_body so Ollama dynamically sizes
    the KV cache. The run stops at the first input failure (typically VRAM
    exhaustion).
"""

import argparse
import contextlib
import functools
import json
import os
import secrets
import statistics
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import openai

from util.bazel.workspace import get_build_working_directory

OLLAMA_BASE_URL = "https://ollama.allegedly.works/v1"
DEFAULT_MODELS = ["gpt-oss-20b-128k", "gpt-oss-120b-128k"]

TIME_LIMIT_S = 60
MIN_SAMPLES = 10
NIAH_SAMPLES_DEFAULT = 8

_FILLER_PHRASE = "The quick brown fox jumps over the lazy dog near the riverbank. "
_CHARS_PER_TOKEN = 4.5

CONTEXT_SIZES = [1_000, 4_000, 16_000, 32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000]

OUTPUT_MAX_TOKENS = 128
OUTPUT_SEED_PROMPT = "Write a detailed essay about the history of computing:"

# War and Peace (Tolstoy) from Project Gutenberg — ~3.3 MB, covers all context sizes.
_GUTENBERG_URL = "https://www.gutenberg.org/files/2600/2600-0.txt"
_HAYSTACK_CACHE_PATH = Path.home() / ".cache" / "niah_haystack_war_and_peace.txt"

_NEEDLE_TEMPLATE = "IMPORTANT: The secret passcode hidden in this document is [{code}]. Remember this passcode."
_NIAH_QUESTION = (
    "What is the secret passcode hidden in this document? Respond with only the 8-character passcode, nothing else."
)


def make_client() -> openai.OpenAI:
    api_key = os.environ.get("OLLAMA_API_KEY") or "no-key"
    return openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key=api_key)


def num_ctx_for(target_tokens: int) -> int:
    return int(target_tokens * 1.15 + 512)


def check_model_available(client: openai.OpenAI, model: str) -> bool:
    models = {m.id for m in client.models.list()}
    return model in models


# -- Prompt builders --------------------------------------------------------


def make_filler_prompt(target_tokens: int) -> str:
    target_chars = int(target_tokens * _CHARS_PER_TOKEN)
    full_repeats = target_chars // len(_FILLER_PHRASE)
    remainder = target_chars % len(_FILLER_PHRASE)
    return _FILLER_PHRASE * full_repeats + _FILLER_PHRASE[:remainder]


@functools.cache
def _get_haystack_text() -> str:
    if _HAYSTACK_CACHE_PATH.exists():
        return _HAYSTACK_CACHE_PATH.read_text(encoding="utf-8")
    print("  Downloading haystack text from Project Gutenberg...", flush=True)
    _HAYSTACK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_GUTENBERG_URL) as resp:
        text: str = resp.read().decode("utf-8")
    _HAYSTACK_CACHE_PATH.write_text(text, encoding="utf-8")
    print(f"  Cached to {_HAYSTACK_CACHE_PATH} ({len(text):,} chars)", flush=True)
    return text


def _make_haystack(target_chars: int) -> str:
    source = _get_haystack_text()
    if len(source) >= target_chars:
        return source[:target_chars]
    repeats = target_chars // len(source) + 1
    return (source * repeats)[:target_chars]


def _build_niah_prompt(context_size_tokens: int, needle_code: str, depth_frac: float) -> str:
    target_chars = int(context_size_tokens * _CHARS_PER_TOKEN)
    haystack = _make_haystack(target_chars)
    needle = _NEEDLE_TEMPLATE.format(code=needle_code)
    insert_pos = int(len(haystack) * depth_frac)
    boundary = haystack.rfind(". ", 0, insert_pos)
    if boundary == -1:
        boundary = insert_pos
    else:
        boundary += 2
    text = haystack[:boundary] + needle + " " + haystack[boundary:]
    return text + "\n\n" + _NIAH_QUESTION


# -- Data types -------------------------------------------------------------


@dataclass
class NiahSample:
    timestamp: str
    model: str
    context_size_tokens: int
    depth_frac: float
    needle_code: str
    prompt_char_len: int
    prompt_tokens: int | None
    response_text: str
    found: bool
    elapsed_s: float
    num_ctx: int


# -- Benchmark functions ----------------------------------------------------


def prewarm(client: openai.OpenAI, model: str, num_ctx: int) -> float:
    """Send a throwaway request to load model and allocate KV cache. Returns seconds elapsed."""
    start = time.monotonic()
    with contextlib.suppress(openai.APIError):
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=1,
            stream=False,
            extra_body={"options": {"num_ctx": num_ctx}},
        )
    return time.monotonic() - start


def unload_model(client: openai.OpenAI, model: str) -> None:
    """Send keep_alive=0 to unload model from GPU memory."""
    with contextlib.suppress(openai.APIError):
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "x"}],
            max_tokens=1,
            stream=False,
            extra_body={"keep_alive": 0},
        )


def measure_output(client: openai.OpenAI, model: str, time_limit: float) -> list[float]:
    """Measure output (decode) speed: tokens/sec between first and last streaming chunk."""
    results: list[float] = []
    deadline = time.monotonic() + time_limit

    while time.monotonic() < deadline and len(results) < MIN_SAMPLES:
        first_ts: float | None = None
        last_ts: float | None = None
        token_count = 0

        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": OUTPUT_SEED_PROMPT}],
            max_tokens=OUTPUT_MAX_TOKENS,
            stream=True,
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


def measure_input(client: openai.OpenAI, model: str, target_tokens: int, time_limit: float) -> list[float]:
    """Measure input (prefill) speed: prompt_tokens / wall_clock.

    Assumes KV cache is already allocated at the right num_ctx (call prewarm first).
    """
    prompt = make_filler_prompt(target_tokens)
    results: list[float] = []
    deadline = time.monotonic() + time_limit
    num_ctx = num_ctx_for(target_tokens)

    while time.monotonic() < deadline and len(results) < MIN_SAMPLES:
        start = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1,
                stream=False,
                extra_body={"options": {"num_ctx": num_ctx}},
            )
        except openai.APIError as exc:
            print(f" [error: {exc}]", flush=True)
            break
        elapsed = time.monotonic() - start
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
        if prompt_tokens and elapsed > 0:
            results.append(prompt_tokens / elapsed)

    return results


def measure_decode_at_context(client: openai.OpenAI, model: str, target_tokens: int, time_limit: float) -> list[float]:
    """Measure decode speed after prefilling a large context.

    Sends a filler prompt of ~target_tokens followed by a question that elicits
    a multi-token response. Measures output token rate from streaming chunks.
    """
    prompt = make_filler_prompt(target_tokens) + "\n\nNow write a short paragraph about the weather."
    results: list[float] = []
    deadline = time.monotonic() + time_limit
    num_ctx = num_ctx_for(target_tokens)

    while time.monotonic() < deadline and len(results) < MIN_SAMPLES:
        first_ts: float | None = None
        last_ts: float | None = None
        token_count = 0

        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=OUTPUT_MAX_TOKENS,
                stream=True,
                extra_body={"options": {"num_ctx": num_ctx}},
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
        except openai.APIError as exc:
            print(f" [error: {exc}]", flush=True)
            break

        if first_ts and last_ts and last_ts > first_ts and token_count > 1:
            results.append((token_count - 1) / (last_ts - first_ts))

    return results


def run_niah(
    client: openai.OpenAI, model: str, context_size_tokens: int, max_samples: int, time_limit: float
) -> list[NiahSample]:
    """Run NIAH samples at evenly spaced depths until time_limit or max_samples reached."""
    samples: list[NiahSample] = []
    num_ctx = num_ctx_for(context_size_tokens)
    deadline = time.monotonic() + time_limit
    depths = [i / (max_samples - 1) for i in range(max_samples)] if max_samples > 1 else [0.5]

    while time.monotonic() < deadline and len(samples) < max_samples:
        depth_frac = depths[len(samples)]
        needle_code = secrets.token_hex(4)
        prompt_text = _build_niah_prompt(context_size_tokens, needle_code, depth_frac)

        # Stream the response. gpt-oss is a reasoning model: LiteLLM strips
        # thinking tokens from the non-streaming content field, so streaming
        # is required to capture the full output for recall scoring.
        start = time.monotonic()
        chunks: list[str] = []
        prompt_tokens: int | None = None
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt_text}],
                max_tokens=1024,
                temperature=0.0,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"options": {"num_ctx": num_ctx}},
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    chunks.append(delta)
                if chunk.usage:
                    prompt_tokens = chunk.usage.prompt_tokens
            elapsed = time.monotonic() - start
            response_text = "".join(chunks)
        except openai.APIError as exc:
            elapsed = time.monotonic() - start
            response_text = f"[API ERROR: {exc}]"

        found = needle_code.lower() in response_text.lower()
        sample = NiahSample(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            model=model,
            context_size_tokens=context_size_tokens,
            depth_frac=round(depth_frac, 4),
            needle_code=needle_code,
            prompt_char_len=len(prompt_text),
            prompt_tokens=prompt_tokens,
            response_text=response_text,
            found=found,
            elapsed_s=round(elapsed, 3),
            num_ctx=num_ctx,
        )
        samples.append(sample)
        status = "\u2713" if found else "\u2717"
        print(
            f"      [{len(samples)}/{max_samples}] depth={depth_frac:.2f} code={needle_code} {status} ({elapsed:.1f}s)",
            flush=True,
        )

    return samples


# -- Formatting helpers -----------------------------------------------------


def _fmt(values: list[float]) -> str:
    if not values:
        return "N/A"
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return f"{mean:.1f} \u00b1 {stdev:.1f} (n={len(values)})"


def _fmt_short(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{statistics.mean(values):.1f} t/s"


def _fmt_niah(samples: list[NiahSample]) -> str:
    if not samples:
        return "N/A"
    found = sum(s.found for s in samples)
    return f"{found}/{len(samples)}"


# -- Main flow --------------------------------------------------------------


def benchmark_model(
    client: openai.OpenAI, model: str, context_sizes: list[int], time_limit: float, niah_samples: int, log_path: Path
) -> tuple[dict[str, list[float]], dict[int, list[NiahSample]]]:
    print(f"\n{'=' * 60}", flush=True)
    print(f"Model: {model}", flush=True)
    print(f"{'=' * 60}", flush=True)

    if not check_model_available(client, model):
        print("  SKIP: model not available (not yet pulled?)")
        return {}, {}

    results: dict[str, list[float]] = {}
    niah_results: dict[int, list[NiahSample]] = {}

    # Output speed (uses server default num_ctx).
    print("  prewarm (loading model)...", end=" ", flush=True)
    warmup_s = prewarm(client, model, num_ctx_for(context_sizes[0]))
    print(f"{warmup_s:.1f}s", flush=True)

    print(f"  output (up to {time_limit:.0f}s)...", end=" ", flush=True)
    output = measure_output(client, model, time_limit)
    results["output"] = output
    print(_fmt(output), flush=True)

    for ctx in context_sizes:
        ctx_label = f"{ctx // 1000}k"
        num_ctx = num_ctx_for(ctx)

        # Prewarm: allocate KV cache at this num_ctx (pays reallocation cost once).
        print(f"  prewarm {ctx_label} (num_ctx={num_ctx})...", end=" ", flush=True)
        warmup_s = prewarm(client, model, num_ctx)
        print(f"{warmup_s:.1f}s", flush=True)

        # Input speed (KV cache already allocated).
        print(f"  input {ctx_label} (up to {time_limit:.0f}s)...", end=" ", flush=True)
        inp = measure_input(client, model, ctx, time_limit)
        results[f"input{ctx_label}"] = inp
        print(_fmt(inp), flush=True)
        if not inp:
            print(f"    (stopped \u2014 likely hit VRAM limit at {ctx:,} tokens)", flush=True)
            break

        # Decode speed at this context size (KV cache filled, then generate).
        print(f"  decode {ctx_label} (up to {time_limit:.0f}s)...", end=" ", flush=True)
        dec = measure_decode_at_context(client, model, ctx, time_limit)
        results[f"decode{ctx_label}"] = dec
        print(_fmt(dec), flush=True)

        # NIAH recall (same num_ctx, no reallocation needed).
        if niah_samples > 0:
            print(f"  niah {ctx_label} (up to {niah_samples} samples, {time_limit:.0f}s)...", flush=True)
            niah = run_niah(client, model, ctx, niah_samples, time_limit)
            niah_results[ctx] = niah
            found = sum(s.found for s in niah)
            print(f"    recall: {found}/{len(niah)}", flush=True)

            with log_path.open("a") as f:
                for sample in niah:
                    f.write(json.dumps(asdict(sample)) + "\n")

    return results, niah_results


def print_summary(
    all_results: dict[str, dict[str, list[float]]],
    all_niah: dict[str, dict[int, list[NiahSample]]],
    models: list[str],
    context_sizes: list[int],
) -> None:
    col = 28

    # Build metric list: output, then interleaved (input, decode, niah) per context size.
    metrics: list[str] = ["output"]
    for s in context_sizes:
        label = f"{s // 1000}k"
        metrics.append(f"input{label}")
        metrics.append(f"decode{label}")
        metrics.append(f"niah{label}")

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    header = f"{'Metric':<12}" + "".join(f"{m:<{col}}" for m in models)
    print(header)
    print("-" * len(header))

    for metric in metrics:
        row = f"{metric:<12}"
        any_data = False
        for m in models:
            if metric.startswith("niah"):
                ctx_k = int(metric.removeprefix("niah").removesuffix("k"))
                samples = all_niah.get(m, {}).get(ctx_k * 1000, [])
                cell = _fmt_niah(samples)
                if samples:
                    any_data = True
            else:
                vals = all_results.get(m, {}).get(metric, [])
                cell = _fmt_short(vals)
                if vals:
                    any_data = True
            row += f"{cell:<{col}}"
        if any_data:
            print(row)

    print()
    print("Notes:")
    print("  output  = decode speed at minimal context (output tok/s, streaming)")
    print("  input   = prefill speed (input tok/s, prompt_tokens / wall_clock)")
    print("  decode  = decode speed after prefilling N tokens (output tok/s, streaming)")
    print("  niah    = needle-in-haystack recall (found/total, evenly spaced depths)")
    print(f"  Each config ran for up to {TIME_LIMIT_S}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Ollama models via LiteLLM proxy")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--context-sizes", nargs="+", type=int, default=CONTEXT_SIZES)
    parser.add_argument("--time-limit", type=float, default=TIME_LIMIT_S, help="Seconds per config")
    parser.add_argument(
        "--niah-samples", type=int, default=NIAH_SAMPLES_DEFAULT, help="NIAH samples per ctx (0=disable)"
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None, help="Directory for JSONL logs (default: next to this script)"
    )
    parser.add_argument("--unload-between-models", action="store_true", help="Unload model from GPU between models")
    args = parser.parse_args()

    client = make_client()

    build_wd = get_build_working_directory()
    if args.log_dir:
        raw = args.log_dir
        log_dir = raw if raw.is_absolute() else build_wd / raw
    else:
        log_dir = build_wd / "hack" / "benchmark_ollama"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = time.strftime("%Y%m%d_%H%M%S")

    print(f"Ollama benchmark \u2014 {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Endpoint:     {OLLAMA_BASE_URL}")
    print(f"Models:       {', '.join(args.models)}")
    print(f"Context sizes: {args.context_sizes}")
    print(f"NIAH samples: {args.niah_samples}")
    print(f"Time limit:   {args.time_limit:.0f}s per config")
    print(f"Log dir:      {log_dir}")

    all_results: dict[str, dict[str, list[float]]] = {}
    all_niah: dict[str, dict[int, list[NiahSample]]] = {}

    for i, model in enumerate(args.models):
        log_path = log_dir / f"benchmark_{model.replace('/', '_')}_{timestamp_str}.jsonl"

        results, niah = benchmark_model(client, model, args.context_sizes, args.time_limit, args.niah_samples, log_path)
        all_results[model] = results
        all_niah[model] = niah

        if args.unload_between_models and i < len(args.models) - 1:
            print(f"\n  Unloading {model}...", end=" ", flush=True)
            unload_model(client, model)
            print("done", flush=True)

    print_summary(all_results, all_niah, args.models, args.context_sizes)


if __name__ == "__main__":
    main()
