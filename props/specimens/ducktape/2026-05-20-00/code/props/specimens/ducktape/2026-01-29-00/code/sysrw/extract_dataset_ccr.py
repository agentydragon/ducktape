"""
Extract dataset from Claude Code Router logs.

- Input: ~/.claude-code-router/logs/trace.*
- Output: ./data/dataset_ccr.jsonl (one JSON object per line)

Selection logic:
- Only consider inbound_request events that carry Anthropic-style body
- Keep samples where:
  * System includes the tools header string (constants.TOOLS_HEADER), and
  * The last user message contains the BAD_MARKER token "<bad>"

Each output record has shape:
{
  "correlation_id": str | null,
  "timestamp": int | null,
  "anthropic_request": CCRRequest,
  "log_file": path
}
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from sysrw.constants import BAD_MARKER
from sysrw.extract_common import (
    find_last_user_text_from_msg as find_last_user_text,
    sys_has_tools_header,
    write_jsonl_batches,
)

TRACE_DIR = Path.home() / ".claude-code-router" / "logs"
OUTPUT_PATH = Path(__file__).parent / "data" / "dataset_ccr.jsonl"


async def process_file(p: Path) -> list[dict]:
    out: list[dict] = []
    lines = 0
    kept = 0
    last_tick = time.monotonic()
    interval = 2.0  # seconds
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            lines += 1
            rec = json.loads(line)
            if rec.get("event") != "inbound_request":
                continue
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            if not sys_has_tools_header(body.get("system")):
                continue
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                continue
            # Strict: marker must appear in the last message, and that last message must be a user text message
            last_msg = messages[-1]
            last_text = find_last_user_text(last_msg)
            if not last_text or BAD_MARKER not in last_text:
                continue
            out.append(
                {
                    "correlation_id": rec.get("correlationId"),
                    "timestamp": rec.get("timestamp"),
                    "anthropic_request": body,
                    "log_file": str(p),
                }
            )
            kept += 1
            now = time.monotonic()
            if now - last_tick >= interval:
                print(json.dumps({"event": "progress", "file": str(p), "lines": lines, "kept": kept}))
                last_tick = now
    print(json.dumps({"event": "file_done", "file": str(p), "lines": lines, "kept": kept}))
    return out


async def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted(TRACE_DIR.glob("trace.*")) if p.is_file()]
    sem = asyncio.Semaphore(16)

    async def wrapped(p: Path):
        async with sem:
            return await process_file(p)

    results: list[list[dict]] = await asyncio.gather(*[wrapped(p) for p in files])
    write_jsonl_batches(results, OUTPUT_PATH, event="dataset_ccr_written")


# Console entrypoint
def cli():
    asyncio.run(main())
