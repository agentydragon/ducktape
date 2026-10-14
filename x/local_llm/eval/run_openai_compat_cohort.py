"""Run the frozen HumanEval+ chat cohort through an OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROMPTS_SHA256 = "6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd"
SEEDS = (20261013, 20261014, 20261015)
OPTIONS = {"temperature": 0.2, "top_p": 0.95, "max_completion_tokens": 2048, "reasoning_effort": "none"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _write_new_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _post_json(url: str, payload: dict[str, object], timeout: float) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        url, data=_json_bytes(payload), headers={"Content-Type": "application/json"}, method="POST"
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode(errors='replace')}") from error
    elapsed = time.perf_counter() - start
    if not isinstance(value, dict):
        raise ValueError("endpoint returned a non-object JSON response")
    return value, elapsed


def _load_prompts(prompts_path: Path, cohort_path: Path) -> tuple[list[dict[str, Any]], str]:
    if _sha256(prompts_path.read_bytes()) != PROMPTS_SHA256:
        raise ValueError(f"frozen prompts do not match the expected SHA-256: {prompts_path}")
    cohort = _read_object(cohort_path)
    task_ids = cohort.get("task_ids")
    if not isinstance(task_ids, list) or not all(isinstance(value, str) for value in task_ids):
        raise ValueError("cohort task_ids must be a list of strings")
    records = []
    for line_number, line in enumerate(prompts_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
            raise ValueError(f"{prompts_path}:{line_number}: expected a record with chat messages")
        if record.get("task_id") in task_ids:
            records.append(record)
    records.sort(key=lambda record: task_ids.index(record["task_id"]))
    if [record.get("task_id") for record in records] != task_ids:
        raise ValueError("frozen prompts do not cover the cohort task_ids in order")
    return records, _sha256(cohort_path.read_bytes())


def _request_body(model: str, record: dict[str, Any], seed: int) -> dict[str, object]:
    messages = record["messages"]
    if not isinstance(messages, list):
        raise ValueError("prompt messages must be a list")
    return {"model": model, "messages": messages, **OPTIONS, "seed": seed, "stream": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible API root, including /v1")
    parser.add_argument("--model", required=True, help="endpoint model identifier")
    parser.add_argument("--output", type=Path, required=True, help="append-only raw-response JSONL")
    parser.add_argument("--prompts", type=Path, required=True, help="frozen prompt JSONL")
    parser.add_argument("--cohort", type=Path, required=True, help="frozen cohort manifest JSON")
    parser.add_argument("--request-timeout-seconds", type=float, default=300)
    parser.add_argument("--deadline-utc", help="stop before another request if its timeout crosses this ISO UTC time")
    parser.add_argument("--reserve-seconds", type=float, default=30)
    parser.add_argument("--max-new-requests", type=int, help="bounded first-run gate; resume preserves exact samples")
    args = parser.parse_args()
    parts = urlsplit(args.base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        parser.error("--base-url must be an HTTP(S) URL without credentials")
    if args.request_timeout_seconds <= 0 or args.reserve_seconds < 0:
        parser.error("timeout must be positive and reserve nonnegative")
    if args.max_new_requests is not None and args.max_new_requests < 1:
        parser.error("--max-new-requests must be positive")
    deadline = datetime.fromisoformat(args.deadline_utc) if args.deadline_utc else None
    if deadline is not None and deadline.tzinfo is None:
        parser.error("--deadline-utc needs a timezone, for example Z")
    if deadline is not None:
        deadline = deadline.astimezone(UTC)

    prompts, cohort_hash = _load_prompts(args.prompts, args.cohort)
    config = {
        "schema": 1,
        "endpoint": "/chat/completions",
        "base_url": args.base_url.rstrip("/"),
        "requested_model": args.model,
        "prompts_sha256": PROMPTS_SHA256,
        "cohort_sha256": cohort_hash,
        "options": OPTIONS,
        "seeds_requested": list(SEEDS),
        "seed_effective_support": "unknown; the endpoint response does not confirm whether seed was honored",
        "request_order": "task-major, three repeats back-to-back; later repeats may benefit from prompt caching",
        "stream": False,
        "request_timeout_seconds": args.request_timeout_seconds,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(str(args.output) + ".config.json")
    inflight_path = Path(str(args.output) + ".inflight.json")
    if config_path.exists():
        if _read_object(config_path) != config:
            raise ValueError(f"resume configuration mismatch: {config_path}")
    elif args.output.exists() or inflight_path.exists():
        raise ValueError("raw output or in-flight marker exists without configuration manifest")
    else:
        _write_new_json(config_path, config)

    rows: list[dict[str, Any]] = []
    if args.output.exists():
        for line_number, line in enumerate(args.output.read_text().splitlines(), start=1):
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"{args.output}:{line_number}: expected a JSON object")
                rows.append(record)
    expected = {(record["task_id"], repeat) for record in prompts for repeat in range(3)}
    completed: set[tuple[str, int]] = set()
    for row in rows:
        key = (row.get("task_id"), row.get("repeat"))
        if key not in expected or key in completed or row.get("status") != "ok":
            raise ValueError(f"invalid, duplicate, or failed prior sample: {key}")
        prompt = next(record for record in prompts if record["task_id"] == key[0])
        body = _request_body(args.model, prompt, SEEDS[int(key[1])])
        if row.get("request_sha256") != _sha256(_json_bytes(body)):
            raise ValueError(f"sample does not match frozen request parameters: {key}")
        completed.add(key)
    if inflight_path.exists():
        inflight = _read_object(inflight_path)
        key = (inflight.get("task_id"), inflight.get("repeat"))
        if key not in completed:
            raise ValueError(f"interrupted request {key} has unknown outcome; refusing silent retry")
        inflight_path.unlink()

    emitted = 0
    endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
    with args.output.open("a", encoding="utf-8") as output:
        for prompt in prompts:
            for repeat, seed in enumerate(SEEDS):
                key = (prompt["task_id"], repeat)
                if key in completed:
                    continue
                if args.max_new_requests is not None and emitted >= args.max_new_requests:
                    print(json.dumps({"event": "stopped_gate", "completed": len(completed), "total": len(expected)}))
                    return 0
                if deadline is not None and (deadline - datetime.now(UTC)).total_seconds() < (
                    args.request_timeout_seconds + args.reserve_seconds
                ):
                    print(
                        json.dumps({"event": "stopped_deadline", "completed": len(completed), "total": len(expected)})
                    )
                    return 0
                body = _request_body(args.model, prompt, seed)
                request_hash = _sha256(_json_bytes(body))
                _write_new_json(inflight_path, {"task_id": key[0], "repeat": repeat, "request_sha256": request_hash})
                started = datetime.now(UTC)
                response, elapsed = _post_json(endpoint, body, args.request_timeout_seconds)
                choices = response.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise ValueError("chat completion lacks choices[0]")
                choice = choices[0]
                message = choice.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise ValueError("chat completion lacks assistant text content")
                content = message["content"]
                usage = response.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                row = {
                    "task_id": key[0],
                    "repeat": repeat,
                    "seed_requested": seed,
                    "seed_effective_support": "unknown",
                    "status": "ok",
                    "started_at_utc": started.isoformat(),
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                    "wall_seconds": elapsed,
                    "model": response.get("model"),
                    "system_fingerprint": response.get("system_fingerprint"),
                    "finish_reason": choice.get("finish_reason"),
                    "input_tokens": usage.get("prompt_tokens"),
                    "generated_tokens": usage.get("completion_tokens"),
                    "response": content,
                    "response_json": response,
                    "prompt_sha256": prompt["prompt_sha256"],
                    "request_sha256": request_hash,
                }
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                inflight_path.unlink()
                completed.add(key)
                emitted += 1
                print(
                    json.dumps(
                        {
                            "event": "generation",
                            "task_id": key[0],
                            "repeat": repeat,
                            "wall_seconds": elapsed,
                            "completion_tokens": usage.get("completion_tokens"),
                            "finish_reason": choice.get("finish_reason"),
                        }
                    ),
                    flush=True,
                )
    print(json.dumps({"event": "complete", "completed": len(completed), "total": len(expected)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
