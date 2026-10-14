"""Bounded OpenVINO GenAI throughput probe on rugged's NPU."""

import json
import os
import resource
import sys
import time
from pathlib import Path

import openvino as ov
import openvino_genai as ov_genai


def rss_kib() -> int:
    with Path("/proc/self/status").open(encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise RuntimeError("VmRSS missing from /proc/self/status")


def accel_fds() -> list[str]:
    paths = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            path = str(fd.readlink())
        except FileNotFoundError:
            continue  # /proc/self/fd includes the directory handle used by listdir.
        if path.startswith("/dev/accel/"):
            paths.append(path)
    return paths


def main() -> None:
    model_path = sys.argv[1]
    core = ov.Core()
    devices = core.available_devices
    if "NPU" not in devices:
        raise RuntimeError(f"NPU missing: {devices=}")
    device_name = core.get_property("NPU", "FULL_DEVICE_NAME")
    print(
        json.dumps(
            {
                "event": "setup",
                "model_path": model_path,
                "openvino": ov.__version__,
                "openvino_genai": ov_genai.__version__,
                "devices": devices,
                "npu_name": device_name,
                "pid": os.getpid(),
            }
        ),
        flush=True,
    )

    start = time.perf_counter()
    pipe = ov_genai.LLMPipeline(model_path, "NPU", MAX_PROMPT_LEN=512, MIN_RESPONSE_LEN=128)
    print(
        json.dumps(
            {
                "event": "loaded",
                "elapsed_seconds": time.perf_counter() - start,
                "rss_kib": rss_kib(),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "accel_fds": accel_fds(),
            }
        ),
        flush=True,
    )

    short_prompt = "Write a short explanation of why the sky looks blue in daylight."
    long_prompt = (
        "Summarize the following notes: "
        + (
            "At midday sunlight passes through air. Short blue wavelengths scatter more than red wavelengths. "
            "At sunset the light travels farther and the sky near the horizon can look orange. "
        )
        * 8
    )
    for iteration, (prompt, max_new_tokens) in enumerate([(short_prompt, 128)] * 4 + [(long_prompt, 32)] * 3):
        start = time.perf_counter()
        result = pipe.generate([prompt], max_new_tokens=max_new_tokens, do_sample=False)
        metrics = result.perf_metrics
        print(
            json.dumps(
                {
                    "event": "generation",
                    "iteration": iteration,
                    "warmup": iteration == 0,
                    "elapsed_seconds": time.perf_counter() - start,
                    "input_tokens": metrics.get_num_input_tokens(),
                    "generated_tokens": metrics.get_num_generated_tokens(),
                    "ttft_ms": metrics.get_ttft().mean,
                    "tpot_ms": metrics.get_tpot().mean,
                    "throughput_tps": metrics.get_throughput().mean,
                    "generate_duration_ms": metrics.get_generate_duration().mean,
                    "text": result.texts[0],
                    "rss_kib": rss_kib(),
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
