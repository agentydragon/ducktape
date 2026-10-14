"""Bounded, reproducible Ollama long-context probe on rugged.

Uses an isolated Ollama server. It does not start or configure that server.
The Hugging Face tokenizer is used only to size prompts independently; Ollama's
native prompt_eval_count and runner logs are the execution evidence.
"""

import argparse
import hashlib
import json
import os
import random
import re
import signal
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from drm_memory import ollama_runner_drm_memory
from tokenizers import Tokenizer

WORDS = [
    "amber",
    "birch",
    "cedar",
    "delta",
    "ember",
    "frost",
    "grove",
    "harbor",
    "iris",
    "juniper",
    "kelp",
    "lunar",
    "maple",
    "north",
    "ocean",
    "pine",
    "quartz",
    "river",
    "silver",
    "timber",
    "umber",
    "violet",
    "willow",
    "xenon",
]
LABELS = ("FIRST_KEY", "MIDDLE_KEY", "LAST_KEY")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def host_snapshot() -> dict:
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemAvailable", "MemFree", "SwapFree"):
            mem[key + "_kib"] = int(value.split()[0])
    pressure = Path("/proc/pressure/memory").read_text().strip()
    full_line = next(line for line in pressure.splitlines() if line.startswith("full "))
    full_avg10 = float(next(field.split("=", 1)[1] for field in full_line.split() if field.startswith("avg10=")))
    return {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **mem,
        "memory_pressure": pressure,
        "memory_full_avg10_pct": full_avg10,
        "ac_online": Path("/sys/class/power_supply/AC/online").read_text().strip(),
    }


def verify_isolated_server(pid: int, base: str) -> None:
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    expected_host = base.removeprefix("http://").removeprefix("https://")
    hosts = {f"OLLAMA_HOST={expected_host}".encode(), f"OLLAMA_HOST={base}".encode()}
    if b"ollama" not in cmdline or b"serve" not in cmdline or not hosts.intersection(environment):
        raise RuntimeError(f"PID {pid} is not the isolated Ollama server on {expected_host}")


def make_prompt(tokenizer: Tokenizer, target_tokens: int) -> tuple[str, dict]:
    rng = random.Random(20261014)
    # A high-entropy ledger makes position tests meaningful and avoids a giant
    # repeated string that can overstate a model's long-context behavior.
    lines = []
    for i in range(9000):
        words = " ".join(rng.choice(WORDS) for _ in range(14))
        lines.append(f"ROW {i:05d}: {words}.\n")
    head = (
        "Read this ledger. Exactly three lines are marked FIRST_KEY, MIDDLE_KEY, "
        "and LAST_KEY. At the end, return their exact six-digit hexadecimal "
        "codes as a JSON object using those three keys. Do not guess.\n"
    )
    tail = "\nReturn only JSON with FIRST_KEY, MIDDLE_KEY, and LAST_KEY.\n"
    codes = {label: f"{rng.randrange(16**6):06X}" for label in LABELS}

    def assemble(n: int) -> str:
        body = lines[:n].copy()
        for label, position in zip(LABELS, (max(1, n // 100), n // 2, n - max(1, n // 100)), strict=True):
            body[position] = f"{label}: {codes[label]}\n" + body[position]
        return head + "".join(body) + tail

    lo, hi = 100, len(lines)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenizer.encode(assemble(mid)).ids) <= target_tokens:
            lo = mid
        else:
            hi = mid - 1
    prompt = assemble(lo)
    content_tokens = len(tokenizer.encode(prompt).ids)
    if abs(content_tokens - target_tokens) > 100:
        raise RuntimeError(f"prompt length {content_tokens} too far from target {target_tokens}")
    anchor_rows = {"FIRST_KEY": max(1, lo // 100), "MIDDLE_KEY": lo // 2, "LAST_KEY": lo - max(1, lo // 100)}
    return prompt, {
        "target_content_tokens": target_tokens,
        "content_tokens": content_tokens,
        "line_count": lo,
        "expected_codes": codes,
        "anchor_rows": anchor_rows,
        "prompt_sha256": sha256(prompt.encode()),
        "prompt_bytes": len(prompt.encode()),
    }


def api(base: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        base + path,
        None if body is None else json.dumps(body).encode(),
        {} if body is None else {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def monitor(
    stop: threading.Event,
    path: Path,
    stage: str,
    minimum_available_kib: int,
    server_pid: int,
    base: str,
    deadline: datetime,
) -> None:
    while not stop.is_set():
        snapshot = host_snapshot()
        log_jsonl(path, {"stage": stage, "runner_drm": ollama_runner_drm_memory(server_pid), **snapshot})
        reason = None
        if snapshot["MemAvailable_kib"] < minimum_available_kib:
            reason = "low_memory_threshold"
        elif snapshot["memory_full_avg10_pct"] >= 10:
            reason = "memory_pressure_threshold"
        elif datetime.now(UTC) >= deadline:
            reason = "deadline"
        if reason:
            log_jsonl(path, {"stage": stage, "event": reason, **snapshot})
            verify_isolated_server(server_pid, base)
            os.kill(server_pid, signal.SIGTERM)
            return
        stop.wait(2)


def run_request(
    base: str,
    model: str,
    prompt: str,
    num_ctx: int,
    stage: str,
    monitor_path: Path,
    minimum_available_kib: int,
    server_pid: int,
    deadline: datetime,
    max_output_tokens: int = 256,
    num_batch: int | None = None,
) -> dict:
    body = {
        "model": model,
        "stream": True,
        "think": False,
        "keep_alive": "10m",
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_ctx": num_ctx, "num_predict": max_output_tokens, "temperature": 0, "seed": 20261014},
    }
    if num_batch is not None:
        body["options"]["num_batch"] = num_batch
    request_bytes = json.dumps(body).encode()
    stop = threading.Event()
    watcher = threading.Thread(
        target=monitor, args=(stop, monitor_path, stage, minimum_available_kib, server_pid, base, deadline), daemon=True
    )
    watcher.start()
    start = time.perf_counter()
    first_token = None
    chunks = []
    final = None
    try:
        req = urllib.request.Request(base + "/api/chat", request_bytes, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2700) as response:
            for line in response:
                event = json.loads(line)
                if "error" in event:
                    raise RuntimeError(f"Ollama stream error: {event['error']}")
                chunk = event.get("message", {}).get("content", "")
                if chunk:
                    if first_token is None:
                        first_token = time.perf_counter() - start
                    chunks.append(chunk)
                if event.get("done"):
                    final = event
    finally:
        stop.set()
        watcher.join(timeout=2)
    if final is None:
        raise RuntimeError("Ollama stream ended without final usage record")
    return {
        "stage": stage,
        "model": model,
        "request_options": body["options"],
        "minimum_available_kib": minimum_available_kib,
        "deadline_utc": deadline.isoformat(),
        "request_sha256": sha256(request_bytes),
        "wall_seconds": time.perf_counter() - start,
        "first_content_seconds": first_token,
        "response": "".join(chunks),
        "native": final,
        "host_after": host_snapshot(),
        "runner_drm_after": ollama_runner_drm_memory(server_pid),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:11510")
    parser.add_argument("--model", default="qwen3:4b-instruct-2507-q4_K_M")
    parser.add_argument("--num-ctx", type=int, default=131072)
    parser.add_argument("--num-batch", type=int)
    parser.add_argument("--targets", default="32000,64000,124000")
    parser.add_argument("--minimum-available-gib", type=float, default=8.0)
    parser.add_argument("--server-pid", type=int, required=False)
    parser.add_argument("--deadline-utc", default="2026-10-14T08:30:00Z")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    prompts = []
    if not args.load_only:
        tokenizer = Tokenizer.from_file(str(args.tokenizer))
        targets = [int(value) for value in args.targets.split(",")]
        prompts = [(target, *make_prompt(tokenizer, target)) for target in targets]
        manifest = {
            "model": args.model,
            "num_ctx_requested": args.num_ctx,
            "num_batch_requested": args.num_batch,
            "targets": [metadata for _, _, metadata in prompts],
            "tokenizer_sha256": sha256(args.tokenizer.read_bytes()),
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (args.output / "prompt_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.prepare_only:
        return

    if args.server_pid is None:
        raise ValueError("--server-pid is required when running probes")
    verify_isolated_server(args.server_pid, args.base)
    deadline = datetime.fromisoformat(args.deadline_utc)
    if deadline.tzinfo is None:
        raise ValueError("--deadline-utc requires an explicit UTC offset")

    minimum_available_kib = int(args.minimum_available_gib * 2**20)
    if args.load_only:
        try:
            result = run_request(
                args.base,
                args.model,
                "Say ready.",
                args.num_ctx,
                "load",
                args.output / "memory_samples.jsonl",
                minimum_available_kib,
                args.server_pid,
                deadline,
                max_output_tokens=4,
                num_batch=args.num_batch,
            )
        except Exception as exc:
            log_jsonl(
                args.output / "results.jsonl",
                {"stage": "load", "event": "request_error", "error": repr(exc), "host_after": host_snapshot()},
            )
            raise
        result["ps_after"] = api(args.base, "/api/ps")
        log_jsonl(args.output / "results.jsonl", result)
        return
    for target, prompt, metadata in prompts:
        stage = str(target)
        snapshot = host_snapshot()
        if snapshot["MemAvailable_kib"] < minimum_available_kib:
            log_jsonl(args.output / "results.jsonl", {"stage": stage, "event": "skipped_low_memory", **snapshot})
            break
        if snapshot["memory_full_avg10_pct"] >= 10:
            log_jsonl(args.output / "results.jsonl", {"stage": stage, "event": "skipped_memory_pressure", **snapshot})
            break
        if datetime.now(UTC) >= deadline:
            log_jsonl(args.output / "results.jsonl", {"stage": stage, "event": "skipped_deadline"})
            break
        try:
            result = run_request(
                args.base,
                args.model,
                prompt,
                args.num_ctx,
                stage,
                args.output / "memory_samples.jsonl",
                minimum_available_kib,
                args.server_pid,
                deadline,
                num_batch=args.num_batch,
            )
        except Exception as exc:
            log_jsonl(
                args.output / "results.jsonl",
                {
                    "stage": stage,
                    "event": "request_error",
                    "error": repr(exc),
                    "prompt": metadata,
                    "host_after": host_snapshot(),
                },
            )
            raise
        result["prompt"] = metadata
        result["ps_after"] = api(args.base, "/api/ps")
        parsed = re.search(r"\{.*\}", result["response"], re.DOTALL)
        try:
            result["retrieved_codes"] = json.loads(parsed.group()) if parsed else None
        except json.JSONDecodeError:
            result["retrieved_codes"] = None
        result["retrieval_correct"] = result["retrieved_codes"] == metadata["expected_codes"]
        log_jsonl(args.output / "results.jsonl", result)


if __name__ == "__main__":
    main()
