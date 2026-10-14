"""Check pinned Qwen2.5-Coder IR at an agent-sized NPU context."""

import json
import os
import resource
import time
from pathlib import Path

import openvino as ov
import openvino_genai as ov_genai


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
    print(json.dumps({"event": event, **fields}), flush=True)


def main() -> None:
    model = "/tmp/rugged-npu-qwen25coder-15b-sym-ov"
    core = ov.Core()
    devices = core.available_devices
    if "NPU" not in devices:
        raise RuntimeError(f"NPU missing: {devices}")
    emit("setup", model=model, devices=devices, openvino=ov.__version__, genai=ov_genai.__version__)
    start = time.perf_counter()
    pipe = ov_genai.LLMPipeline(
        model, "NPU", MAX_PROMPT_LEN=8192, MIN_RESPONSE_LEN=2048, CACHE_DIR="/tmp/rugged-npu-qwen-cache"
    )
    emit(
        "loaded",
        elapsed_seconds=time.perf_counter() - start,
        rss_kib=rss_kib(),
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        accel_fds=accel_fds(),
        pid=os.getpid(),
    )
    prompt = 'def add(a: int, b: int) -> int:\n    """Return the sum of a and b."""\n'
    start = time.perf_counter()
    result = pipe.generate([prompt], max_new_tokens=128, do_sample=False, apply_chat_template=False)
    metrics = result.perf_metrics
    emit(
        "generation",
        elapsed_seconds=time.perf_counter() - start,
        input_tokens=metrics.get_num_input_tokens(),
        generated_tokens=metrics.get_num_generated_tokens(),
        ttft_ms=metrics.get_ttft().mean,
        throughput_tps=metrics.get_throughput().mean,
        text=result.texts[0],
        rss_kib=rss_kib(),
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        accel_fds=accel_fds(),
    )


if __name__ == "__main__":
    main()
