"""Measure TTFT across longer NPU prompts with one cached Qwen pipeline."""

import json
import resource
import time
from pathlib import Path

import openvino as ov
import openvino_genai as ov_genai

MODEL = "/tmp/rugged-npu-qwen25coder-15b-sym-ov"
CACHE = "/tmp/rugged-npu-qwen-cache"


def rss_kib() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS missing")


def main() -> None:
    core = ov.Core()
    if "NPU" not in core.available_devices:
        raise RuntimeError("NPU unavailable")
    start = time.perf_counter()
    pipe = ov_genai.LLMPipeline(MODEL, "NPU", MAX_PROMPT_LEN=8192, MIN_RESPONSE_LEN=2048, CACHE_DIR=CACHE)
    accelerators = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            accelerators.append(str(fd.readlink()))
        except FileNotFoundError:
            continue
    if "/dev/accel/accel0" not in accelerators:
        raise RuntimeError("pipeline did not open NPU accelerator")
    print(
        json.dumps(
            {
                "event": "loaded",
                "seconds": time.perf_counter() - start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
        ),
        flush=True,
    )

    tokenizer = pipe.get_tokenizer()
    filler = "A stable sort preserves the original order of equal keys. "
    for target in (256, 2048, 4096):
        prompt = "Summarize this note in one sentence:\n" + filler * (target // 10)
        pretokenized_length = len(tokenizer.encode(prompt).input_ids.data[0])
        for repeat in range(3):
            start = time.perf_counter()
            result = pipe.generate([prompt], max_new_tokens=32, do_sample=False)
            metrics = result.perf_metrics
            print(
                json.dumps(
                    {
                        "event": "generation",
                        "target_tokens": target,
                        "pretokenized_length": pretokenized_length,
                        "repeat": repeat,
                        "input_tokens": metrics.get_num_input_tokens(),
                        "generated_tokens": metrics.get_num_generated_tokens(),
                        "ttft_ms": metrics.get_ttft().mean,
                        "tokenization_ms": metrics.get_tokenization_duration().mean,
                        "elapsed_seconds": time.perf_counter() - start,
                        "throughput_tps": metrics.get_throughput().mean,
                        "rss_kib": rss_kib(),
                        "text": result.texts[0],
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
