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
        elif query_arg.endswith("test-query.txt"):
            tests = os.environ.get("FAKE_TESTS", "//ci:normal_test")
            if tests:
                print(tests)
        else:
            raise SystemExit(f"unexpected query file: {query_arg}")
    elif args and args[0] in {"test", "build"}:
        role = args[0]
        with (log_dir / "phases.log").open("a") as log:
            log.write(f"{role}\n")
        if args[0] == "test":
            (log_dir / "test-args.log").write_text("\n".join(args))
            target_file = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--target_pattern_file=")), None)
            if target_file:
                (log_dir / "affected-targets.log").write_text(Path(target_file).read_text())
        else:
            (log_dir / "build-args.log").write_text("\n".join(args))
        raise SystemExit(int(os.environ.get(f"FAKE_{role.upper()}_EXIT", "0")))
    else:
        raise SystemExit(f"unexpected bazel args: {args}")


if __name__ == "__main__":
    main()
