"""Needle-in-haystack (NIAH) recall evaluation for Ollama models.

Embeds a unique secret code at a random position inside a long filler context,
asks the model to retrieve it, and records whether it succeeded.

Usage:
    bazel run //experimental/benchmark_ollama:niah
    bazel run //experimental/benchmark_ollama:niah -- --models gpt-oss-20b-128k \\
        --context-sizes 4000 16000 64000 128000

Output:
    Prints a recall score matrix (context_size x depth_bucket).
    Appends every sample as a JSON line to a timestamped JSONL file in the
    current working directory (or --log-dir) for offline analysis.

Methodology:
    For each (model, context_size, depth_bucket) cell:
      - Sample SAMPLES_PER_CELL needle positions uniformly from the bucket.
      - Build the prompt: haystack text of ~context_size tokens with the needle
        sentence inserted at the target character offset.
      - Stream the response (stream=True, max_tokens=1024). gpt-oss is a
        reasoning model: LiteLLM strips thinking tokens from non-streaming
        content, so streaming is required to capture the full output.
      - Score: 1 if the 8-char code appears anywhere in the full stream
        (reasoning + answer), case-insensitive. P(false positive) ≈ 1/2^32.
    Depth bucket labels: p10/p30/p50/p70/p90 refer to the fraction through the
    document where the needle is placed (centred on 0.1, 0.3, 0.5, 0.7, 0.9).

Environment:
    OLLAMA_API_KEY  API key for ollama.allegedly.works (optional)
"""

import argparse
import functools
import json
import logging
import os
import random
import secrets
import statistics
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import openai

from bazel_util.workspace import get_build_working_directory

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = "https://ollama.allegedly.works/v1"
DEFAULT_MODELS = ["gpt-oss-20b-128k"]

# Depth buckets: label → (low_frac, high_frac) range to sample needle position from.
DEPTH_BUCKETS: dict[str, tuple[float, float]] = {
    "p10": (0.00, 0.20),
    "p30": (0.20, 0.40),
    "p50": (0.40, 0.60),
    "p70": (0.60, 0.80),
    "p90": (0.80, 1.00),
}

DEFAULT_CONTEXT_SIZES = [4_000, 16_000, 32_000, 64_000, 128_000]
SAMPLES_PER_CELL = 5

_CHARS_PER_TOKEN = 4.5  # rough chars-per-token for English wordpiece tokenisers

# War and Peace (Tolstoy) from Project Gutenberg — ~3.3 MB, covers all context sizes.
_GUTENBERG_URL = "https://www.gutenberg.org/files/2600/2600-0.txt"
_HAYSTACK_CACHE_PATH = Path.home() / ".cache" / "niah_haystack_war_and_peace.txt"


@functools.cache
def get_haystack_text() -> str:
    """Return War and Peace text, downloading and caching on first call."""
    if _HAYSTACK_CACHE_PATH.exists():
        return _HAYSTACK_CACHE_PATH.read_text(encoding="utf-8")
    print("Downloading haystack text from Project Gutenberg...", flush=True)
    _HAYSTACK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_GUTENBERG_URL) as resp:
        text = resp.read().decode("utf-8")
    _HAYSTACK_CACHE_PATH.write_text(text, encoding="utf-8")
    print(f"Cached to {_HAYSTACK_CACHE_PATH} ({len(text):,} chars)", flush=True)
    return text


NEEDLE_TEMPLATE = "IMPORTANT: The secret passcode hidden in this document is [{code}]. Remember this passcode."
QUESTION = (
    "What is the secret passcode hidden in this document? Respond with only the 8-character passcode, nothing else."
)


@dataclass
class Sample:
    timestamp: str
    model: str
    context_size_tokens: int
    depth_bucket: str
    depth_frac: float  # exact fraction where needle was placed
    needle_code: str  # the 8-char hex code to find
    prompt_char_len: int
    prompt_tokens: int | None  # from usage, if available
    response_text: str
    found: bool  # needle_code present in response (case-insensitive)
    elapsed_s: float
    num_ctx: int  # num_ctx sent to Ollama


def make_client() -> openai.OpenAI:
    api_key = os.environ.get("OLLAMA_API_KEY") or "no-key"
    return openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key=api_key)


def make_haystack(target_chars: int) -> str:
    """Return target_chars of War and Peace text, tiling if needed."""
    source = get_haystack_text()
    if len(source) >= target_chars:
        return source[:target_chars]
    # Tile in case target exceeds the book length (shouldn't happen with W&P at 3.3 MB).
    repeats = target_chars // len(source) + 1
    return (source * repeats)[:target_chars]


def build_prompt(context_size_tokens: int, needle_code: str, depth_frac: float) -> str:
    """Build a chat user message with the needle embedded at depth_frac."""
    target_chars = int(context_size_tokens * _CHARS_PER_TOKEN)
    haystack = make_haystack(target_chars)
    needle = NEEDLE_TEMPLATE.format(code=needle_code)

    insert_pos = int(len(haystack) * depth_frac)
    # Snap to the nearest sentence boundary (period) to keep prose readable.
    boundary = haystack.rfind(". ", 0, insert_pos)
    if boundary == -1:
        boundary = insert_pos
    else:
        boundary += 2  # after ". "

    text = haystack[:boundary] + needle + " " + haystack[boundary:]
    return text + "\n\n" + QUESTION


def run_sample(
    client: openai.OpenAI, model: str, context_size_tokens: int, depth_bucket: str, rng: random.Random
) -> Sample:
    low, high = DEPTH_BUCKETS[depth_bucket]
    depth_frac = rng.uniform(low, high)
    needle_code = secrets.token_hex(4)  # 8-char lowercase hex, e.g. "a3f72c01"

    num_ctx = int(context_size_tokens * 1.15 + 512)
    prompt_text = build_prompt(context_size_tokens, needle_code, depth_frac)

    # Stream the response. gpt-oss is a reasoning model: LiteLLM strips
    # thinking tokens from the non-streaming content field, so they appear
    # only in streaming delta.content. Searching the full stream is the only
    # reliable way to check recall for this model family.
    start = time.monotonic()
    chunks: list[str] = []
    prompt_tokens: int | None = None
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt_text}],
            max_tokens=1024,  # enough for thinking + short final answer
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

    return Sample(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        model=model,
        context_size_tokens=context_size_tokens,
        depth_bucket=depth_bucket,
        depth_frac=round(depth_frac, 4),
        needle_code=needle_code,
        prompt_char_len=len(prompt_text),
        prompt_tokens=prompt_tokens,
        response_text=response_text,
        found=found,
        elapsed_s=round(elapsed, 3),
        num_ctx=num_ctx,
    )


def check_model_available(client: openai.OpenAI, model: str) -> bool:
    models = {m.id for m in client.models.list()}
    return model in models


def run_niah(
    client: openai.OpenAI,
    model: str,
    context_sizes: list[int],
    samples_per_cell: int,
    log_path: Path,
    rng: random.Random,
) -> dict[tuple[int, str], list[Sample]]:
    """Run the full NIAH grid and return results keyed by (context_size, bucket)."""
    results: dict[tuple[int, str], list[Sample]] = {}

    total_cells = len(context_sizes) * len(DEPTH_BUCKETS)
    cell_idx = 0

    for ctx in context_sizes:
        for bucket in DEPTH_BUCKETS:
            cell_idx += 1
            cell_samples: list[Sample] = []
            print(
                f"  [{cell_idx}/{total_cells}] ctx={ctx // 1000}k  bucket={bucket}  ({samples_per_cell} samples)",
                flush=True,
            )

            for s_idx in range(samples_per_cell):
                sample = run_sample(client, model, ctx, bucket, rng)
                cell_samples.append(sample)

                status = "✓" if sample.found else "✗"
                print(
                    f"    [{s_idx + 1}/{samples_per_cell}] depth={sample.depth_frac:.2f}  "
                    f"code={sample.needle_code}  {status}  "
                    f"resp={sample.response_text[:60]!r}  "
                    f"({sample.elapsed_s:.1f}s)",
                    flush=True,
                )

                # Append to log immediately so partial runs are recoverable.
                with log_path.open("a") as f:
                    f.write(json.dumps(asdict(sample)) + "\n")

            results[(ctx, bucket)] = cell_samples

    return results


def print_score_matrix(results: dict[tuple[int, str], list[Sample]], model: str, context_sizes: list[int]) -> None:
    buckets = list(DEPTH_BUCKETS.keys())
    col_w = 12

    print(f"\n{'=' * 70}")
    print(f"NIAH Recall — {model}")
    print(f"{'=' * 70}")
    header = f"{'ctx':>8}  " + "".join(f"{b:>{col_w}}" for b in buckets) + f"  {'mean':>{col_w}}"
    print(header)
    print("-" * len(header))

    for ctx in context_sizes:
        row_recalls = []
        row = f"{ctx // 1000}k{'':<5}  "
        for bucket in buckets:
            samples = results.get((ctx, bucket), [])
            if samples:
                recall = sum(s.found for s in samples) / len(samples)
                row_recalls.append(recall)
                n = len(samples)
                cell = f"{recall:.0%} ({sum(s.found for s in samples)}/{n})"
            else:
                cell = "N/A"
            row += f"{cell:>{col_w}}"
        if row_recalls:
            mean_recall = statistics.mean(row_recalls)
            row += f"  {mean_recall:.0%}{'':<{col_w - 4}}"
        print(row)

    # Column means
    print("-" * len(header))
    col_means_row = f"{'mean':>8}  "
    for bucket in buckets:
        all_found = [s.found for ctx in context_sizes for s in results.get((ctx, bucket), [])]
        if all_found:
            mean = sum(all_found) / len(all_found)
            col_means_row += f"{mean:.0%}{'':<{col_w - 3}}"
        else:
            col_means_row += f"{'N/A':>{col_w}}"
    print(col_means_row)


def append_results_to_benchmarks_md(
    results: dict[tuple[int, str], list[Sample]], model: str, context_sizes: list[int], benchmarks_md: Path
) -> None:
    """Append a markdown score table to benchmarks.md."""
    buckets = list(DEPTH_BUCKETS.keys())
    date = time.strftime("%Y-%m-%d")
    lines = [
        f"\n### NIAH Recall — {model} ({date})\n",
        "| ctx | " + " | ".join(buckets) + " | mean |",
        "| --: | " + " | ".join(["---:"] * len(buckets)) + " | ---: |",
    ]
    for ctx in context_sizes:
        row_recalls = []
        cells = [f"{ctx // 1000}k"]
        for bucket in buckets:
            samples = results.get((ctx, bucket), [])
            if samples:
                recall = sum(s.found for s in samples) / len(samples)
                row_recalls.append(recall)
                cells.append(f"{recall:.0%} ({sum(s.found for s in samples)}/{len(samples)})")
            else:
                cells.append("N/A")
        if row_recalls:
            cells.append(f"{statistics.mean(row_recalls):.0%}")
        else:
            cells.append("N/A")
        lines.append("| " + " | ".join(cells) + " |")

    block = "\n".join(lines) + "\n"

    content = benchmarks_md.read_text()
    marker = "<!-- Results appended by niah.py -->"
    content = content.replace(marker, block + "\n" + marker) if marker in content else content + block
    benchmarks_md.write_text(content)
    print(f"\nResults appended to {benchmarks_md}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Needle-in-haystack recall eval for Ollama")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--context-sizes", nargs="+", type=int, default=DEFAULT_CONTEXT_SIZES)
    parser.add_argument("--samples", type=int, default=SAMPLES_PER_CELL, help="Samples per cell")
    parser.add_argument("--log-dir", type=Path, default=None, help="Directory for JSONL logs")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--benchmarks-md", type=Path, default=None, help="Path to benchmarks.md to append results to")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    rng = random.Random(args.seed)
    client = make_client()

    build_wd = get_build_working_directory()
    raw_log_dir = args.log_dir or Path()
    log_dir = raw_log_dir if raw_log_dir.is_absolute() else build_wd / raw_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"NIAH eval — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Endpoint:      {OLLAMA_BASE_URL}")
    print(f"Models:        {', '.join(args.models)}")
    print(f"Context sizes: {args.context_sizes}")
    print(f"Depth buckets: {list(DEPTH_BUCKETS)}")
    print(f"Samples/cell:  {args.samples}")

    for model in args.models:
        print(f"\n{'=' * 60}")
        print(f"Model: {model}")
        print(f"{'=' * 60}")

        if not check_model_available(client, model):
            print("  SKIP: model not available")
            continue

        log_path = log_dir / f"niah_results_{model.replace('/', '_')}_{timestamp}.jsonl"
        print(f"Logging to: {log_path}")

        results = run_niah(
            client=client,
            model=model,
            context_sizes=args.context_sizes,
            samples_per_cell=args.samples,
            log_path=log_path,
            rng=rng,
        )

        print_score_matrix(results, model, args.context_sizes)

        # Find benchmarks.md. Under Bazel, __file__ is in runfiles (read-only),
        # so we prefer --benchmarks-md or a path relative to --log-dir.
        if args.benchmarks_md:
            bmd = args.benchmarks_md
        elif args.log_dir:
            bmd = args.log_dir / "benchmarks.md"
        else:
            bmd = Path("benchmarks.md")

        if bmd.exists():
            append_results_to_benchmarks_md(results, model, args.context_sizes, bmd)


if __name__ == "__main__":
    main()
