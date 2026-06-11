"""
Arg0 runner for exposing virtual CLIs via argv[0].

Currently supports:
- apply_patch / applypatch: Patch dispatcher (OpenAI envelope or unified diff, multi-file)

This module is intended to be invoked via symlink or wrapper named after the
desired command. It may also accept a fallback flag: --adgn-run-as <name>.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arg0_runner.patch import apply_patch_auto


def _safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError
    return p


def _apply_patch_cli(argv: list[str]) -> int:
    # Read patch from arg or stdin
    patch_text = argv[1] if len(argv) >= 2 and argv[1] not in ("-", "--") else sys.stdin.read()
    if not patch_text:
        print("apply_patch: missing patch text (arg or stdin)", file=sys.stderr)
        return 2
    root = Path.cwd().resolve()

    def _open_fn(path: str) -> str:
        p = _safe_join(root, path)
        return p.read_text(encoding="utf-8")

    def _write_fn(path: str, content: str) -> None:
        p = _safe_join(root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _remove_fn(path: str) -> None:
        p = _safe_join(root, path)
        if p.exists():
            p.unlink()

    # Single black-box dispatcher; allow multi-file patches in CLI
    apply_patch_auto(patch_text, _open_fn, _write_fn, _remove_fn, require_single_file=False)
    return 0


def main(argv: list[str] | None = None, *, argv0: str | None = None) -> int:
    argv = list(sys.argv) if argv is None else argv
    exe = Path(argv0 or argv[0])
    name = exe.stem
    # Fallback flag
    if len(argv) >= 3 and argv[1] == "--adgn-run-as":
        name = argv[2]
        argv = [argv[0], *argv[3:]]
    if name in ("apply_patch", "applypatch"):
        return _apply_patch_cli(argv)
    print(f"arg0_runner: unknown command: {name}", file=sys.stderr)
    return 127


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
