"""
Extract a CCR-like dataset from Crush provider wire logs without touching the
existing dataset.jsonl used by eval.

- Inputs:
  * Single file: --wire-log PATH (or env CRUSH_WIRE_LOG)
  * Scan mode: --scan-dir DIR (repeatable); finds **/.crush/logs/provider-wire.log under these roots
- Output: ./data/dataset_crush.jsonl (one JSON object per line)
  Does NOT overwrite ./data/dataset.jsonl

Selection logic (mirrors CCR extractor heuristics where possible):
- Only consider provider wire records where direction == "request"
- Expect payload to be an OpenAI Chat/Responses request (messages/system)
- Keep samples where:
  * System includes the tools header string, and
  * The last user message contains the BAD_MARKER token "<bad>"

Each output record has shape:
{
  "correlation_id": str | null,   # message_id or session_id from wire
  "timestamp": int | null,        # epoch millis parsed from RFC3339 ts
  "oai_request": { ... },         # original request payload captured by Crush
  "wirelog": {                    # minimal provenance/debug
      "event_type": str,          # e.g. chat.completions.new_streaming
      "path": ".../provider-wire.log"
  }
}
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sysrw.constants import BAD_MARKER
from sysrw.extract_common import (
    find_last_user_text_from_messages,
    iter_wire_lines,
    maybe_extract_payload,
    sys_has_tools_header,
)

ROOT = Path(__file__).parent
DEFAULT_CRUSH_DIR = Path.home() / "code" / "crush"
DEFAULT_WIRE_LOG = (
    Path(os.environ.get("CRUSH_WIRE_LOG", ""))
    if os.environ.get("CRUSH_WIRE_LOG")
    else (Path.home() / ".crush" / "logs" / "provider" / "provider-wire.log")
)
OUTPUT_PATH = ROOT / "data" / "dataset_crush.jsonl"


def parse_rfc3339_millis(ts: str | None) -> int | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Support fractional seconds
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _extract_input_messages(payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, bool]:
    """Extract messages from Responses API payload (input array)."""
    inp = payload.get("input")
    if not isinstance(inp, list):
        return None, False
    msgs: list[dict[str, Any]] = []
    has_header = False
    for item in inp:
        if not isinstance(item, dict):
            continue
        role = (item.get("role") or item.get("message_role") or item.get("Role") or "").lower()
        content = item.get("content")
        # Extract text from either string or list-of-parts
        if isinstance(content, str):
            text = content
        else:
            texts: list[str] = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        t = c.get("text") or c.get("input_text") or c.get("content")
                        if isinstance(t, str):
                            texts.append(t)
            text = "\n".join(texts) if texts else ""
        # Heuristic: if role missing, assume leading items are system until we see a 'user' later
        if not role:
            role = "system" if not any(m.get("role") == "user" for m in msgs) else "assistant"
        if role in ("system", "user", "assistant") and text:
            if role == "system" and sys_has_tools_header(text):
                has_header = True
            msgs.append({"role": role, "content": text})
    return (msgs if msgs else None), has_header


def _extract_chat_messages(payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, bool]:
    """Extract messages from Chat Completions payload (messages array)."""
    arr = payload.get("messages")
    if not isinstance(arr, list):
        return None, False
    msgs: list[dict[str, Any]] = []
    has_header = False
    for m in arr:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").lower()
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # list of parts: {type: "text", text: "..."}
            texts: list[str] = []
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    texts.append(c["text"])
            text = "\n".join(texts) if texts else ""
        else:
            text = ""
        if role in ("system", "user", "assistant") and text:
            if role == "system" and sys_has_tools_header(text):
                has_header = True
            msgs.append({"role": role, "content": text})
    return (msgs if msgs else None), has_header


def process_wire(path: Path, require_bad: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lines = 0
    kept = 0
    last_tick = time.monotonic()
    interval = 2.0
    for line in iter_wire_lines(path):
        lines += 1
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("direction") != "request":
            continue
        payload = maybe_extract_payload(e)
        if not payload:
            continue
        # Extract messages from either Responses or Chat payloads
        messages = None
        if isinstance(payload, dict) and "input" in payload:
            messages, _ = _extract_input_messages(payload)
        elif isinstance(payload, dict) and "messages" in payload:
            messages, _ = _extract_chat_messages(payload)
        else:
            continue
        last_text = find_last_user_text_from_messages(messages)
        if require_bad and (not last_text or BAD_MARKER not in last_text):
            continue
        out.append(
            {
                "timestamp": parse_rfc3339_millis(e.get("ts")),
                "oai_request": payload,
                "wirelog": {"event_type": e.get("event_type"), "path": str(path)},
            }
        )
        kept += 1
        now = time.monotonic()
        if now - last_tick >= interval:
            print(json.dumps({"event": "progress", "file": str(path), "lines": lines, "kept": kept}))
            last_tick = now
    print(json.dumps({"event": "file_done", "file": str(path), "lines": lines, "kept": kept}))
    return out


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Extract dataset from Crush provider wire logs")
    ap.add_argument(
        "--wire-log", type=Path, default=DEFAULT_WIRE_LOG, help="Path to provider-wire.log (overrides scan mode)"
    )
    ap.add_argument(
        "--scan-dir",
        action="append",
        type=Path,
        default=[],
        help="Scan DIR recursively for **/.crush/logs/provider-wire.log (repeatable)",
    )
    ap.add_argument(
        "--output", type=Path, default=OUTPUT_PATH, help="Output JSONL path (default: ./data/dataset_crush.jsonl)"
    )
    return ap.parse_args()


def find_wire_logs(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    patterns = ("**/provider-wire.log", "**/provider-wire-*.log", "**/provider-wire-*.log.gz")
    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            for p in root.glob(pat):
                found.append(p)
    # Dedup and sort
    return sorted({p.resolve() for p in found})


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    logs: list[Path] = []
    if args.wire_log and isinstance(args.wire_log, Path) and str(args.wire_log):
        logs = [args.wire_log]
    else:
        roots = args.scan_dir or [Path.home() / "code"]
        logs = find_wire_logs(roots)

    total = 0
    with args.output.open("w", encoding="utf-8") as out:
        for log_path in logs:
            recs = process_wire(log_path, require_bad=True)
            for r in recs:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(recs)
    print(
        json.dumps(
            {"event": "dataset_crush_written", "count": total, "path": str(args.output), "files_scanned": len(logs)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
