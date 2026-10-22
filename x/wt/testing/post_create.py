#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path


def main() -> None:
    # Verify stdin is valid (raises if fd 0 is bad)
    sys.stdin.fileno()

    p = argparse.ArgumentParser()
    p.add_argument("--worktree_root", required=True)
    p.add_argument("--worktree_name", required=True)
    args, _ = p.parse_known_args()

    root = Path(args.worktree_root)
    if not root.exists():
        print("worktree root does not exist", file=sys.stderr)
        raise SystemExit(3)

    print("py post-create: hello from stdout")
    print("py post-create: hello from stderr", file=sys.stderr)

    (root / ".py_post_create_ran").write_text("ok")


if __name__ == "__main__":
    main()
