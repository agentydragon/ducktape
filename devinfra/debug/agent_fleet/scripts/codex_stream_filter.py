#!/usr/bin/env python3
# Filter codex `exec --json` stdout into one short line per milestone, for Monitor.
# Covers success AND failure signatures so silence never masks a crash.
import json
import sys

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        continue
    t = o.get("type", "")
    it = o.get("item", {}) if isinstance(o.get("item"), dict) else {}
    itype = it.get("type", "")
    if t == "thread.started":
        print(f"WORKER thread={o.get('thread_id')}", flush=True)
    elif itype in ("command_execution", "local_shell_call", "exec_command"):
        cmd = (it.get("command") or it.get("cmd") or "")[:60]
        print(f"WORKER ran: {cmd}", flush=True)
    elif itype in ("file_change", "patch"):
        print("WORKER edited files", flush=True)
    elif itype in ("agent_message", "assistant_message"):
        print(f"WORKER says: {(it.get('text') or '')[:120]}", flush=True)
    elif itype == "error":
        print(f"WORKER ERROR: {(it.get('message') or '')[:120]}", flush=True)
    elif t == "turn.completed":
        print("WORKER turn.completed", flush=True)
    elif t == "turn.failed":
        print(f"WORKER turn.failed: {json.dumps(o.get('error', {}))[:120]}", flush=True)
