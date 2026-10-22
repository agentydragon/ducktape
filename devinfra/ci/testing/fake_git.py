#!/bin/python
import os
import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:2] == ["rev-parse", "HEAD"]:
        print("merge")
    elif args[:2] == ["rev-parse", "merge^1"]:
        print("base")
    elif args[:2] == ["rev-parse", "merge^2"]:
        print("pr-head")
    elif args[:2] == ["diff", "--name-only"]:
        print(os.environ["CHANGED_FILE"])
    elif args and (args[0] == "fetch" or "checkout" in args):
        if "checkout" in args and "--force" not in args:
            raise SystemExit(f"checkout must discard generated changes: {args}")
    else:
        raise SystemExit(f"unexpected git args: {args}")


if __name__ == "__main__":
    main()
