"""Run the frozen EvalPlus HumanEval+ chat cohort on rugged's NPU."""

import argparse
import hashlib
import json
import os
import resource
import time
from pathlib import Path

import openvino as ov
import openvino_genai as ov_genai

MODEL = Path("/tmp/rugged-npu-qwen25coder-15b-sym-ov")
CACHE = Path("/tmp/rugged-npu-qwen-cache")
PROMPTS_SHA256 = "6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd"
SEEDS = (20261013, 20261014, 20261015)


def rss_kib() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS missing")


def accel_fds() -> list[str]:
    paths = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = str(fd.readlink())
        except FileNotFoundError:
            continue
        if target.startswith("/dev/accel/"):
            paths.append(target)
    return paths


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompts", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--max-runs", type=int, default=60)
    args = parser.parse_args()

    prompt_bytes = args.prompts.read_bytes()
    digest = hashlib.sha256(prompt_bytes).hexdigest()
    if digest != PROMPTS_SHA256:
        raise ValueError(f"frozen prompt hash mismatch: {digest}")
    records = [json.loads(line) for line in prompt_bytes.splitlines()]
    if len(records) != 20 or len({r["task_id"] for r in records}) != 20:
        raise ValueError("expected exactly 20 distinct frozen HumanEval+ tasks")
    previous = [json.loads(line) for line in args.results.read_text().splitlines()] if args.results.exists() else []
    completed = {(r["task_id"], r["repeat"]) for r in previous}

    core = ov.Core()
    if "NPU" not in core.available_devices:
        raise RuntimeError(f"NPU unavailable: {core.available_devices}")
    emit("setup", openvino=ov.__version__, genai=ov_genai.__version__, devices=core.available_devices)
    start = time.perf_counter()
    pipe = ov_genai.LLMPipeline(str(MODEL), "NPU", MAX_PROMPT_LEN=8192, MIN_RESPONSE_LEN=2048, CACHE_DIR=str(CACHE))
    if "/dev/accel/accel0" not in accel_fds():
        raise RuntimeError("pipeline did not open the NPU accelerator")
    emit(
        "loaded",
        seconds=time.perf_counter() - start,
        pid=os.getpid(),
        rss_kib=rss_kib(),
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        accel_fds=accel_fds(),
    )
    tokenizer = pipe.get_tokenizer()
    produced = 0
    with args.results.open("a", encoding="utf-8") as output:
        for record in records:
            for repeat, seed in enumerate(SEEDS):
                if (record["task_id"], repeat) in completed:
                    continue
                formatted = tokenizer.apply_chat_template(record["messages"], True)
                start = time.perf_counter()
                result = pipe.generate(
                    [formatted],
                    max_new_tokens=2048,
                    do_sample=True,
                    temperature=0.2,
                    top_p=0.95,
                    rng_seed=seed,
                    apply_chat_template=False,
                )
                metrics = result.perf_metrics
                row = {
                    "task_id": record["task_id"],
                    "repeat": repeat,
                    "seed": seed,
                    "prompt_sha256": record["prompt_sha256"],
                    "response": result.texts[0],
                    "elapsed_seconds": time.perf_counter() - start,
                    "input_tokens": metrics.get_num_input_tokens(),
                    "generated_tokens": metrics.get_num_generated_tokens(),
                    "ttft_ms": metrics.get_ttft().mean,
                    "throughput_tps": metrics.get_throughput().mean,
                    "rss_kib": rss_kib(),
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "accel_fds": accel_fds(),
                }
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
                produced += 1
                emit(
                    "generation",
                    task_id=record["task_id"],
                    repeat=repeat,
                    **{
                        k: row[k]
                        for k in (
                            "elapsed_seconds",
                            "input_tokens",
                            "generated_tokens",
                            "ttft_ms",
                            "throughput_tps",
                            "rss_kib",
                        )
                    },
                )
                if produced >= args.max_runs:
                    return


if __name__ == "__main__":
    main()
