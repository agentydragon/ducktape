#!/bin/python
import os
import re
import sys
from pathlib import Path


def main() -> None:
    log_dir = Path(os.environ["CI_TEST_LOG_DIR"])
    args = sys.argv[1:]
    query_arg = next((arg for arg in args if arg.startswith("--query_file=")), None)
    if query_arg:
        query = Path(query_arg.split("=", 1)[1]).read_text()
        labels = list(dict.fromkeys(re.findall(r'"(//[^"]+)"', query)))
        if query_arg.endswith("affected-query.txt"):
            (log_dir / "query.log").write_text(query)
            if 'except attr("tags", "manual", set(' not in query:
                raise SystemExit("manual exclusion missing")
            labels = [
                label for label in labels if not label.endswith(":manual_test") and not label.endswith(":source.py")
            ]
            print("\n".join(sorted(labels)))
        elif query_arg.endswith("go-test-query.txt"):
            (log_dir / "go-test-query.log").write_text(query)
            print(os.environ.get("FAKE_GO_TESTS", ""))
        elif query_arg.endswith("test-query.txt"):
            print("//ci:normal_test")
        else:
            raise SystemExit(f"unexpected query file: {query_arg}")
    elif args and args[0] in {"test", "build"}:
        if "--@io_bazel_rules_go//go/config:race" in args:
            (log_dir / "race-args.log").write_text("\n".join(args))
            target_file = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--target_pattern_file="))
            (log_dir / "race-targets.log").write_text(Path(target_file).read_text())
            raise SystemExit(int(os.environ.get("FAKE_RACE_EXIT", "0")))
        if args[0] == "test":
            (log_dir / "test-args.log").write_text("\n".join(args))
            target_file = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--target_pattern_file=")), None)
            if target_file:
                (log_dir / "affected-targets.log").write_text(Path(target_file).read_text())
        else:
            (log_dir / "build-args.log").write_text("\n".join(args))
    else:
        raise SystemExit(f"unexpected bazel args: {args}")


if __name__ == "__main__":
    main()
