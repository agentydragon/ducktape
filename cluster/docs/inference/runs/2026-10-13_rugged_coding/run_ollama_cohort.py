"""Run the frozen HumanEval+ chat cohort through Ollama's native API.

This records one logical sample per (task, repeat). An interrupted request is
left in an in-flight journal and is never silently retried on resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

PROMPTS_SHA256 = "6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd"
SEEDS = (20261013, 20261014, 20261015)
OPTIONS = {"temperature": 0.2, "top_p": 0.95, "num_predict": 2048, "num_ctx": 32768}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Ollama origin, e.g. http://127.0.0.1:11436")
    parser.add_argument("--model", required=True, help="installed Ollama model name/tag")
    parser.add_argument("--output", type=Path, required=True, help="append-only raw JSONL")
    parser.add_argument("--prompts", type=Path, default=Path(__file__).with_name("humaneval_plus_prompts.jsonl"))
    parser.add_argument("--cohort", type=Path, default=Path(__file__).with_name("humaneval_plus_cohort.json"))
    parser.add_argument("--deadline-utc", help="stop before another request if its timeout crosses this ISO UTC time")
    parser.add_argument("--request-timeout-seconds", type=float, default=300)
    parser.add_argument("--reserve-seconds", type=float, default=30)
    parser.add_argument(
        "--max-new-requests", type=int, help="bounded first-run gate; later resume keeps the same samples"
    )
    args = parser.parse_args()
    if args.request_timeout_seconds <= 0 or args.reserve_seconds < 0:
        parser.error("timeout must be positive and reserve nonnegative")
    if args.max_new_requests is not None and args.max_new_requests < 1:
        parser.error("--max-new-requests must be positive")
    parts = urlsplit(args.base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        parser.error("--base-url must be an HTTP(S) origin without credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        parser.error("--base-url must not include a path, query, or fragment")
    args.base_url = args.base_url.rstrip("/")
    args.deadline = None
    if args.deadline_utc:
        args.deadline = datetime.fromisoformat(args.deadline_utc)
        if args.deadline.tzinfo is None:
            parser.error("--deadline-utc needs a timezone (for example Z)")
        args.deadline = args.deadline.astimezone(UTC)
    return args


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def request_json(base_url: str, endpoint: str, body: object | None, timeout: float) -> dict:
    request = urllib.request.Request(
        base_url + endpoint,
        data=None if body is None else json_bytes(body),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"{endpoint} returned a non-object JSON value")
    return value


def resolve_model(base_url: str, model: str, timeout: float) -> tuple[str, str]:
    tags = request_json(base_url, "/api/tags", None, timeout).get("models")
    if not isinstance(tags, list):
        raise ValueError("/api/tags response has no models list")
    exact = [tag for tag in tags if isinstance(tag, dict) and model in {tag.get("name"), tag.get("model")}]
    matches = exact or [
        tag for tag in tags if isinstance(tag, dict) and f"{model}:latest" in {tag.get("name"), tag.get("model")}
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("digest"), str):
        raise ValueError(f"model {model!r} did not resolve uniquely with a digest in /api/tags")
    return str(matches[0]["name"]), str(matches[0]["digest"])


def load_frozen_inputs(prompts_path: Path, cohort_path: Path) -> tuple[list[dict], str]:
    raw = prompts_path.read_bytes()
    if sha256(raw) != PROMPTS_SHA256:
        raise ValueError(f"prompt file hash mismatch: {sha256(raw)}")
    prompts = [json.loads(line) for line in raw.splitlines() if line.strip()]
    cohort = json.loads(cohort_path.read_text())
    ids = cohort["task_ids"]
    generation = cohort["generation"]
    if (
        len(prompts) != 20
        or [row["task_id"] for row in prompts] != ids
        or {row["dataset_version"] for row in prompts} != {"v0.1.10"}
        or cohort["evalplus_version"] != "0.3.1"
        or cohort["dataset"] != "humaneval"
        or cohort["dataset_size"] != 164
        or cohort["repeats_per_task"] != 3
        or generation["seed_per_repeat"] != list(SEEDS)
        or generation["temperature"] != OPTIONS["temperature"]
        or generation["top_p"] != OPTIONS["top_p"]
        or generation["max_completion_tokens"] != OPTIONS["num_predict"]
    ):
        raise ValueError("frozen cohort manifest disagrees with the runner")
    for row in prompts:
        if not isinstance(row.get("messages"), list) or len(row["messages"]) != 2:
            raise ValueError(f"invalid frozen messages for {row['task_id']}")
        if sha256(row["prompt"].encode()) != row["prompt_sha256"]:
            raise ValueError(f"prompt hash mismatch for {row['task_id']}")
    return prompts, sha256(cohort_path.read_bytes())


def write_new_json(path: Path, value: object) -> None:
    data = json_bytes(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def append_row(path: Path, row: dict) -> None:
    with path.open("ab") as output:
        output.write(json_bytes(row) + b"\n")
        output.flush()
        os.fsync(output.fileno())


def power_snapshot() -> dict[str, str | None]:
    paths = {
        "ac_online": Path("/sys/class/power_supply/AC/online"),
        "bat0_percent": Path("/sys/class/power_supply/BAT0/capacity"),
        "bat1_percent": Path("/sys/class/power_supply/BAT1/capacity"),
        "platform_profile": Path("/sys/firmware/acpi/platform_profile"),
    }
    values = {}
    for key, path in paths.items():
        try:
            values[key] = path.read_text().strip()
        except OSError:
            values[key] = None
    return values


def chat_body(model: str, record: dict, seed: int) -> dict:
    return {
        "model": model,
        "messages": record["messages"],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {**OPTIONS, "seed": seed},
    }


def main() -> int:
    args = parse_args()
    prompts, cohort_hash = load_frozen_inputs(args.prompts, args.cohort)
    resolved_model, digest = resolve_model(args.base_url, args.model, args.request_timeout_seconds)
    show = request_json(args.base_url, "/api/show", {"model": resolved_model}, args.request_timeout_seconds)
    thinking_metadata = show.get("thinking")
    think_false_supported = None
    if isinstance(thinking_metadata, dict) and isinstance(thinking_metadata.get("values"), list):
        think_false_supported = any(value is False for value in thinking_metadata["values"])
        if not think_false_supported:
            raise ValueError(f"/api/show says model {resolved_model!r} does not support think:false")
    config = {
        "schema": 1,
        "endpoint": "/api/chat",
        "base_url": args.base_url,
        "requested_model": args.model,
        "resolved_model": resolved_model,
        "model_digest": digest,
        "prompts_sha256": PROMPTS_SHA256,
        "cohort_sha256": cohort_hash,
        "options_except_seed": OPTIONS,
        "seeds": list(SEEDS),
        "request_order": "task-major, three repeats back-to-back; later repeats can reuse prompt cache",
        "think_requested": False,
        "think_false_reported_supported": think_false_supported,
        "model_capabilities_reported": show.get("capabilities"),
        "thinking_support_reported": thinking_metadata,
        "stream": False,
        "keep_alive": "30m",
        "request_timeout_seconds": args.request_timeout_seconds,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(str(args.output) + ".config.json")
    inflight_path = Path(str(args.output) + ".inflight.json")
    if config_path.exists():
        if json.loads(config_path.read_text()) != config:
            raise ValueError(f"resume configuration mismatch: {config_path}")
    elif args.output.exists() or inflight_path.exists():
        raise ValueError("raw output or in-flight marker exists without a configuration manifest")
    else:
        write_new_json(config_path, config)

    rows = (
        [json.loads(line) for line in args.output.read_bytes().splitlines() if line.strip()]
        if args.output.exists()
        else []
    )
    expected = {(record["task_id"], repeat): record for record in prompts for repeat in range(3)}
    completed: set[tuple[str, int]] = set()
    for row in rows:
        key = (row["task_id"], row["repeat"])
        if key not in expected or key in completed or row.get("model_digest") != digest:
            raise ValueError(f"invalid or duplicate result row: {key}")
        if row.get("status") != "ok":
            raise ValueError(f"prior failed request requires manual review before resume: {key}")
        record = expected[key]
        if (
            row.get("seed") != SEEDS[key[1]]
            or row.get("prompt_sha256") != record["prompt_sha256"]
            or row.get("request_sha256") != sha256(json_bytes(chat_body(resolved_model, record, SEEDS[key[1]])))
        ):
            raise ValueError(f"result row does not match frozen request: {key}")
        completed.add(key)

    if inflight_path.exists():
        inflight = json.loads(inflight_path.read_text())
        key = (inflight["task_id"], inflight["repeat"])
        if key not in completed or inflight.get("request_sha256") != sha256(
            json_bytes(chat_body(resolved_model, expected[key], SEEDS[key[1]]))
        ):
            raise ValueError(f"interrupted request {key} has unknown outcome; refusing silent retry")
        inflight_path.unlink()

    emitted = 0
    for record in prompts:
        for repeat, seed in enumerate(SEEDS):
            key = (record["task_id"], repeat)
            if key in completed:
                continue
            if args.max_new_requests is not None and emitted >= args.max_new_requests:
                print(
                    json.dumps({"event": "stopped_gate", "completed": len(completed), "total": len(expected)}),
                    flush=True,
                )
                return 0
            if args.deadline and (args.deadline - datetime.now(UTC)).total_seconds() < (
                args.request_timeout_seconds + args.reserve_seconds
            ):
                print(
                    json.dumps({"event": "stopped_deadline", "completed": len(completed), "total": len(expected)}),
                    flush=True,
                )
                return 0
            current_model, current_digest = resolve_model(args.base_url, args.model, args.request_timeout_seconds)
            if (current_model, current_digest) != (resolved_model, digest):
                raise ValueError("model name/digest changed during cohort")
            body = chat_body(resolved_model, record, seed)
            request_hash = sha256(json_bytes(body))
            power_before = power_snapshot()
            write_new_json(inflight_path, {"task_id": key[0], "repeat": repeat, "request_sha256": request_hash})
            started = datetime.now(UTC).isoformat()
            start = time.perf_counter()
            response = None
            try:
                response = request_json(args.base_url, "/api/chat", body, args.request_timeout_seconds)
                elapsed = time.perf_counter() - start
                message = response.get("message")
                if response.get("done") is not True or not isinstance(message, dict):
                    raise ValueError("/api/chat returned an incomplete response")
                content = message.get("content")
                thinking = message.get("thinking") or ""
                if not isinstance(content, str) or not isinstance(thinking, str):
                    raise ValueError("/api/chat returned invalid content or thinking")
                if response.get("model") not in {args.model, resolved_model}:
                    raise ValueError(f"/api/chat responded with unexpected model {response.get('model')!r}")
                native_metrics = {
                    name: value
                    for name, value in response.items()
                    if name.endswith(("_count", "_duration")) or name in {"total_duration", "load_duration"}
                }
                ps_after_first_generation = None
                if not completed:
                    try:
                        ps_after_first_generation = request_json(
                            args.base_url, "/api/ps", None, args.request_timeout_seconds
                        )
                    except Exception as error:
                        ps_after_first_generation = {"error_type": type(error).__name__, "error": str(error)[:1000]}
                row = {
                    "status": "ok",
                    "task_id": key[0],
                    "repeat": repeat,
                    "seed": seed,
                    "prompt_sha256": record["prompt_sha256"],
                    "request_sha256": request_hash,
                    "model": resolved_model,
                    "model_digest": digest,
                    "response": content,
                    "thinking": thinking,
                    "done_reason": response.get("done_reason"),
                    "native_metrics": native_metrics,
                    "native_response": response,
                    "wall_seconds": elapsed,
                    "started_at_utc": started,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                    "power_before": power_before,
                    "power_after": power_snapshot(),
                    "requested_options": body["options"],
                    "think_requested": False,
                }
                if ps_after_first_generation is not None:
                    row["ollama_ps_after_first_generation"] = ps_after_first_generation
            except Exception as error:
                row = {
                    "status": "error",
                    "task_id": key[0],
                    "repeat": repeat,
                    "seed": seed,
                    "request_sha256": request_hash,
                    "model_digest": digest,
                    "error_type": type(error).__name__,
                    "error": str(error)[:1000],
                    "native_response": response,
                    "wall_seconds": time.perf_counter() - start,
                    "started_at_utc": started,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                }
                append_row(args.output, row)
                inflight_path.unlink()
                print(
                    json.dumps({"event": "error", "task_id": key[0], "repeat": repeat, "error": row["error"]}),
                    flush=True,
                )
                return 1
            append_row(args.output, row)
            inflight_path.unlink()
            completed.add(key)
            emitted += 1
            print(
                json.dumps(
                    {
                        "event": "generation",
                        "task_id": key[0],
                        "repeat": repeat,
                        "wall_seconds": row["wall_seconds"],
                        "eval_count": native_metrics.get("eval_count"),
                        "done_reason": row["done_reason"],
                    }
                ),
                flush=True,
            )
    print(json.dumps({"event": "complete", "completed": len(completed), "total": len(expected)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
