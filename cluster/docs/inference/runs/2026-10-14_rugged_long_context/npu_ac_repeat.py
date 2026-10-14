"""Repeat the pinned OpenVINO NPU probe on AC using the GPU speed prompts."""

import argparse
import hashlib
import json
import os
import resource
import time
from pathlib import Path

import openvino as ov
import openvino_genai as genai
from drm_memory import process_drm_memory

MODEL = "/tmp/rugged-npu-qwen25coder-15b-sym-ov"
CACHE = "/tmp/rugged-npu-qwen-cache"


def rss_kib() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS missing")


def available_memory_kib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("MemAvailable missing")


def accel_fds() -> list[str]:
    result = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = str(fd.readlink())
        except FileNotFoundError:
            continue
        if target.startswith("/dev/accel/"):
            result.append(target)
    return result


def emit(path: Path, row: dict) -> None:
    print(json.dumps(row), flush=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("requests", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if available_memory_kib() < 8 * 2**20:
        raise RuntimeError("less than 8 GiB available before NPU pipeline load")
    core = ov.Core()
    if "NPU" not in core.available_devices:
        raise RuntimeError(f"NPU missing: {core.available_devices}")
    emit(
        args.output,
        {
            "event": "setup",
            "openvino": ov.__version__,
            "genai": genai.__version__,
            "model_path": MODEL,
            "max_prompt_len": 8192,
            "min_response_len": 256,
            "npu_name": core.get_property("NPU", "FULL_DEVICE_NAME"),
            "ac_online": Path("/sys/class/power_supply/AC/online").read_text().strip(),
            "mem_available_kib": available_memory_kib(),
        },
    )
    start = time.perf_counter()
    pipe = genai.LLMPipeline(MODEL, "NPU", MAX_PROMPT_LEN=8192, MIN_RESPONSE_LEN=256, CACHE_DIR=CACHE)
    if "/dev/accel/accel0" not in accel_fds():
        raise RuntimeError("pipeline did not open NPU device")
    emit(
        args.output,
        {
            "event": "loaded",
            "seconds": time.perf_counter() - start,
            "rss_kib": rss_kib(),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "accel_fds": accel_fds(),
            "drm_memory": process_drm_memory(os.getpid()),
            "mem_available_kib": available_memory_kib(),
        },
    )
    tokenizer = pipe.get_tokenizer()
    for size in (384, 3072):
        for repeat in (1, 2, 3):
            if available_memory_kib() < 8 * 2**20:
                raise RuntimeError("less than 8 GiB available before NPU generation")
            filename = f"words-{size}-repeat-{repeat}-request.json"
            data = (args.requests / filename).read_bytes()
            request = json.loads(data)
            prompt = tokenizer.apply_chat_template(request["messages"], True)
            start = time.perf_counter()
            result = pipe.generate([prompt], max_new_tokens=256, do_sample=False, apply_chat_template=False)
            metrics = result.perf_metrics
            emit(
                args.output,
                {
                    "event": "generation",
                    "size_words": size,
                    "repeat": repeat,
                    "source_request_sha256": hashlib.sha256(data).hexdigest(),
                    "max_new_tokens": 256,
                    "do_sample": False,
                    "input_tokens": metrics.get_num_input_tokens(),
                    "generated_tokens": metrics.get_num_generated_tokens(),
                    "ttft_ms": metrics.get_ttft().mean,
                    "tpot_ms": metrics.get_tpot().mean,
                    "throughput_tps": metrics.get_throughput().mean,
                    "wall_seconds": time.perf_counter() - start,
                    "rss_kib": rss_kib(),
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "accel_fds": accel_fds(),
                    "drm_memory": process_drm_memory(os.getpid()),
                    "mem_available_kib": available_memory_kib(),
                    "ac_online": Path("/sys/class/power_supply/AC/online").read_text().strip(),
                    "response": result.texts[0],
                },
            )


if __name__ == "__main__":
    main()
