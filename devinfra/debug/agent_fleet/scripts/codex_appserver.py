#!/usr/bin/env python3
"""Minimal driver for `codex app-server` (stdio JSON-RPC) — codex 0.150.1.

Demonstrates the bidirectional loop: initialize handshake, thread/start,
turn/start, streaming notifications, auto-answering server->client approval
requests, and (via modes) turn/steer, turn/interrupt, multi-thread, and a
mid-session reasoning-effort change.

Framing: newline-delimited JSON objects (one JSON value per line), no
Content-Length headers, and the `jsonrpc` field is omitted (app-server style).

Config via env (see the runbook):
  CODEX_BIN, CODEX_FLEET_HOME, CODEX_WORKDIR_ROOT, CODEX_WORKER_MODEL,
  CODEX_WORKER_EFFORT, LITELLM_BASE_URL, and LITELLM_API_KEY (or LITELLM_KEY_FILE).
"""

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

CODEX = os.environ.get("CODEX_BIN", "codex")
CHOME = Path(os.environ.get("CODEX_FLEET_HOME", "~/.cache/codex-fleet")).expanduser()
ROOT = Path(os.environ.get("CODEX_WORKDIR_ROOT", str(CHOME / "work")))
MODEL = os.environ.get("CODEX_WORKER_MODEL", "chatgpt/oai-responses/gpt-5.6-luna")
EFFORT = os.environ.get("CODEX_WORKER_EFFORT", "low")
BASE_URL = os.environ.get("LITELLM_BASE_URL", "https://litellm.allegedly.works/v1")

os.environ["CODEX_HOME"] = str(CHOME)
if not os.environ.get("LITELLM_API_KEY") and os.environ.get("LITELLM_KEY_FILE"):
    os.environ["LITELLM_API_KEY"] = Path(os.environ["LITELLM_KEY_FILE"]).read_text().strip()
assert os.environ.get("LITELLM_API_KEY"), "set LITELLM_API_KEY or LITELLM_KEY_FILE"

# app-server refuses to start if CODEX_HOME is missing — bootstrap it (idempotent).
CHOME.mkdir(parents=True, exist_ok=True)
ROOT.mkdir(parents=True, exist_ok=True)
_cfg = CHOME / "config.toml"
if not _cfg.exists():
    _cfg.write_text(
        f'model = "{MODEL}"\n'
        'model_provider = "litellm"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        f'model_reasoning_effort = "{EFFORT}"\n'
        "model_context_window = 372000\n"
        "[model_providers.litellm]\n"
        'name = "Cluster LiteLLM"\n'
        f'base_url = "{BASE_URL}"\n'
        'env_key = "LITELLM_API_KEY"\n'
        'wire_api = "responses"\n'
    )


class AppServer:
    """One `codex app-server` process with a reader thread and a request/notify API."""

    def __init__(self):
        self.p = subprocess.Popen(
            [CODEX, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._ids = itertools.count(1)
        self.resp = {}  # id -> response message
        self.notes = queue.Queue()  # server notifications
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _read(self):
        for raw in self.p.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"[NONJSON] {line[:120]}", file=sys.stderr)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self.resp[msg["id"]] = msg
            elif "id" in msg and "method" in msg:  # server->client REQUEST
                self._on_server_request(msg)
            elif "method" in msg:  # notification
                self.notes.put(msg)

    def _drain_err(self):
        for raw in self.p.stderr:
            if raw.strip():
                print(f"[stderr] {raw.strip()[:160]}", file=sys.stderr)

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None, timeout=90):
        rid = next(self._ids)
        self._send({"id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self.resp:
                return self.resp.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(f"no response to {method} (id={rid})")

    def notify(self, method, params=None):
        self._send({"method": method, "params": params or {}})

    def _on_server_request(self, msg):
        # Auto-approve everything so the worker runs unattended. (With
        # approvalPolicy=never + danger-full-access these never fire, but a
        # tighter policy would route real approvals here.)
        m = msg["method"]
        print(f"[SERVER->US request] {m}")
        accept_kind = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
        decision = "accept" if m in accept_kind else "approved"
        self._send({"id": msg["id"], "result": {"decision": decision}})

    def drain_notes(self, until, timeout=120):
        """Print notifications until `method` is in `until` (str or set). Returns that note."""
        wanted = {until} if isinstance(until, str) else set(until)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                n = self.notes.get(timeout=0.5)
            except queue.Empty:
                continue
            m = n.get("method")
            p = n.get("params", {})
            if m == "item/completed":
                it = p.get("item", {})
                print(f"  · {m}: {str(it.get('text') or it.get('type', ''))[:100]}")
            elif m in ("turn/started", "turn/completed", "thread/started", "turn/failed", "error"):
                st = (p.get("turn") or {}).get("status")
                print(f"  · {m}{(' status=' + st) if st else ''}: {json.dumps(p)[:90]}")
            if m in wanted:
                return n
        raise TimeoutError(f"timed out waiting for {wanted}")

    def count_reasoning(self, terminal, timeout=120):
        """Drain a turn, counting reasoning items (a rough proxy for how hard it thought)."""
        n = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                note = self.notes.get(timeout=0.5)
            except queue.Empty:
                continue
            m = note.get("method")
            it = (note.get("params") or {}).get("item") or {}
            if m == "item/completed" and it.get("type") == "reasoning":
                n += 1
            if m in terminal:
                return n
        raise TimeoutError("count_reasoning timed out")


def start_thread(a, workdir):
    Path(workdir).mkdir(parents=True, exist_ok=True)
    r = a.request("thread/start", {"cwd": str(workdir), "sandbox": "danger-full-access", "approvalPolicy": "never"})
    return ((r.get("result") or {}).get("thread") or {}).get("id")


def start_turn(a, tid, text, effort=None):
    # `effort` (low|medium|high|xhigh) overrides reasoning effort for THIS turn and
    # subsequent turns on the thread — i.e. reasoning effort is changeable mid-session.
    params = {"threadId": tid, "input": [{"type": "text", "text": text}]}
    if effort:
        params["effort"] = effort
    r = a.request("turn/start", params)
    return ((r.get("result") or {}).get("turn") or {}).get("id")


def connect():
    a = AppServer()
    a.request("initialize", {"clientInfo": {"name": "cc-orchestrator", "version": "1.0"}, "capabilities": {}})
    a.notify("initialized")
    return a


TERMINAL = {"turn/completed", "turn/failed"}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "basic"
    a = connect()
    work = ROOT / "appserver_work"

    if mode == "basic":
        tid = start_thread(a, work)
        start_turn(a, tid, "Create counter.py with value() returning 1. Reply exactly APPSERVER_OK.")
        a.drain_notes("turn/completed", 90)
        print("RESULT counter.py exists:", (work / "counter.py").exists())

    elif mode == "steer":
        tid = start_thread(a, work)
        tn = start_turn(
            a, tid, "Create a.txt, b.txt, c.txt one at a time; each a 3-line poem. Think briefly before each."
        )
        print(f"steer: injecting an extra requirement mid-turn (expectedTurnId={tn})")
        time.sleep(1.5)
        sr = a.request(
            "turn/steer",
            {
                "threadId": tid,
                "expectedTurnId": tn,
                "input": [
                    {"type": "text", "text": "ALSO create STEERED.txt containing the word QUACK after the poems."}
                ],
            },
        )
        print("steer response:", json.dumps(sr.get("result", sr))[:100])
        a.drain_notes(TERMINAL, 120)
        steered = work / "STEERED.txt"
        print(
            "RESULT STEERED.txt exists:",
            steered.exists(),
            "| content:",
            steered.read_text().strip()[:20] if steered.exists() else None,
        )

    elif mode == "interrupt":
        tid = start_thread(a, work)
        tn = start_turn(
            a, tid, "Write numbers 1..40 to numbers.txt ONE per step, with a reasoning sentence before each. Go slowly."
        )
        time.sleep(2.5)
        ir = a.request("turn/interrupt", {"threadId": tid, "turnId": tn})
        print("interrupt response:", json.dumps(ir.get("result", ir))[:100])
        n = a.drain_notes(TERMINAL, 60)
        print(
            "RESULT terminal method:",
            n.get("method"),
            "status:",
            ((n.get("params") or {}).get("turn") or {}).get("status"),
        )

    elif mode == "effort":
        # Same thread, effort changed mid-session: high on turn 1, low on turn 2.
        tid = start_thread(a, work)
        start_turn(a, tid, "Think step by step: what is 17*23? Brief reasoning, then reply P=<product>.", effort="high")
        hi = a.count_reasoning(TERMINAL)
        start_turn(a, tid, "Now add 1 to that product and reply S=<sum>.", effort="low")
        lo = a.count_reasoning(TERMINAL)
        print(f"RESULT reasoning items — high turn: {hi}, low turn: {lo} (effort changed mid-session; both completed)")

    elif mode == "multi":
        w1, w2 = ROOT / "as_t1", ROOT / "as_t2"
        t1, t2 = start_thread(a, w1), start_thread(a, w2)
        print(f"two independent threads: {t1} | {t2}")
        start_turn(a, t1, "Write ONE.txt containing the word ALPHA. Reply DONE1.")
        a.drain_notes("turn/completed", 90)
        start_turn(a, t2, "Write TWO.txt containing the word BRAVO. Reply DONE2.")
        a.drain_notes("turn/completed", 90)
        print("RESULT t1 ONE.txt:", (w1 / "ONE.txt").exists(), "| t2 TWO.txt:", (w2 / "TWO.txt").exists())

    a.p.terminate()
    print(f"=== DONE ({mode}) ===")


if __name__ == "__main__":
    main()
