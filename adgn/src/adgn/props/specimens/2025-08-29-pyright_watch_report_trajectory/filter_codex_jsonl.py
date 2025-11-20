#!/usr/bin/env python3
"""Filter and anonymize Codex JSONL transcript events.

Behavior (hardcoded):
- Drop only these event types (including when nested under msg.type):
  agent_reasoning, turn_diff, exec_command_output_delta
- Anonymize strings in all fields:
  1) Collapse any prefix before "llm/properties/" to that anchor
  2) Replace the repository root with "/ducktape/" (auto-detected via git or provided via --repo-root)
  3) Mask the current system username (via getpass.getuser()) as "<user>" (e.g., ls owner columns)

Usage:
  python3 filter_codex_jsonl.py INPUT.jsonl [--output OUTPUT.jsonl] [--repo-root PATH]

Notes:
- Works with both plain .jsonl and .jsonl.gz (input and/or output)
- Preserves non-JSON lines and still applies anonymization
- Writes a summary to stderr
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import getpass
import gzip
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

# Hardcoded drop types per requirements
DROP_TYPES: set[str] = {"agent_reasoning", "turn_diff", "exec_command_output_delta"}

# Anonymization settings
ANCHOR = "llm/properties/"
# Regex: any path-like prefix ending right before ANCHOR (POSIX-style)
PREFIX_RE = re.compile(r"(?:(?:[A-Za-z]:)?(?:/[\w.+\-@%:,=~]+)*)/" + re.escape(ANCHOR))
# Optional: dynamic repo-root scrubber compiled at runtime
REPO_ROOT_RE: re.Pattern[str] | None = None
RE_USERNAME: re.Pattern[str] | None = None


def open_maybe_gz(path: Path, mode: str):
    text_mode = "t" in mode or "b" not in mode
    if str(path).endswith(".gz"):
        return gzip.open(path, mode, encoding="utf-8" if text_mode else None)
    return path.open(mode, encoding="utf-8" if text_mode else None)


# No parameterization: fixed drop types only


def make_repo_root_re(repo_root: Path) -> re.Pattern[str]:
    root = str(repo_root).rstrip("/")
    return re.compile(re.escape(root) + r"(?:/|$)")


def detect_repo_root(git_cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(git_cwd), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        p = Path(proc.stdout.strip())
        return p if p.exists() else None
    except Exception:
        return None


def anonymize_string(s: str) -> str:
    s = PREFIX_RE.sub(ANCHOR, s)
    if REPO_ROOT_RE is not None:
        s = REPO_ROOT_RE.sub("/ducktape/", s)
    if RE_USERNAME is not None:
        s = RE_USERNAME.sub("<user>", s)
    return s


def transform(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: transform(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [transform(v) for v in obj]
    if isinstance(obj, str):
        return anonymize_string(obj)
    return obj


def should_drop(obj: dict, drop_types: set[str]) -> bool:
    # Drop if top-level type/event matches
    t = str(obj.get("type") or obj.get("event") or "").lower()
    if t in drop_types:
        return True
    # Many Codex events nest details under 'msg'
    msg = obj.get("msg")
    if isinstance(msg, dict):
        mt = str(msg.get("type") or msg.get("event") or "").lower()
        if mt in drop_types:
            return True
    return False


def filter_jsonl(
    inp: Path,
    out: Path,
    drop_types: Iterable[str] = DROP_TYPES,
) -> tuple[int, int, int]:
    drop = {t.lower() for t in drop_types}
    n_in = n_out = n_dropped = 0
    with open_maybe_gz(inp, "rt") as fin, open_maybe_gz(out, "wt") as fout:
        for line in fin:
            n_in += 1
            ln = line.rstrip("\n")
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                # Not JSON: still anonymize path-like strings
                fout.write(anonymize_string(line))
                n_out += 1
                continue
            if should_drop(obj, drop):
                n_dropped += 1
                continue
            obj = transform(obj)
            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n_out += 1
    return n_in, n_out, n_dropped


def default_output_path(inp: Path) -> Path:
    name = inp.name
    if name.endswith(".jsonl.gz"):
        new = name[:-9] + ".filtered.jsonl"  # strip .jsonl.gz
    elif name.endswith(".jsonl"):
        new = name[:-6] + ".filtered.jsonl"
    else:
        new = name + ".filtered"
    return inp.with_name(new)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Explicit repo root to scrub to /ducktape/ (auto-detect via git if omitted)",
    )
    # No --drop-types: this script always drops only agent_reasoning and turn_diff
    args = ap.parse_args(argv)

    inp = args.input
    out = args.output or default_output_path(inp)

    # Initialize dynamic repo-root scrubber and username mask
    global REPO_ROOT_RE, RE_USERNAME
    repo_root = args.repo_root or detect_repo_root(inp.resolve().parent)
    if repo_root is not None:
        REPO_ROOT_RE = make_repo_root_re(repo_root)
    try:
        user = getpass.getuser()
        if user:
            RE_USERNAME = re.compile(rf"(?i)\b{re.escape(user)}\b")
    except Exception:
        RE_USERNAME = None

    n_in, n_out, n_dropped = filter_jsonl(inp, out, DROP_TYPES)

    print(
        f"Filtered {inp} → {out} | in={n_in} kept={n_out} dropped={n_dropped}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
