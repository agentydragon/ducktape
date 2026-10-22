#!/bin/python
import os
import sys
from pathlib import Path


def main() -> None:
    if sys.argv[1] == "get-impacted-targets":
        log_dir = Path(os.environ["CI_TEST_LOG_DIR"])
        (log_dir / "bazel-diff-args.log").write_text("\n".join(sys.argv[1:]))
        print("//ci:normal_test")
        print("//ci:manual_test")
        print("//ci:source.py")
        print("//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir")


if __name__ == "__main__":
    main()
