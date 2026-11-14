#!/bin/python
import os
import sys
from pathlib import Path


def main() -> None:
    if sys.argv[1] == "get-impacted-targets":
        log_dir = Path(os.environ["CI_TEST_LOG_DIR"])
        (log_dir / "bazel-diff-args.log").write_text("\n".join(sys.argv[1:]))
        impacted = os.environ.get(
            "FAKE_IMPACTED_TARGETS",
            "//ci:normal_test\n//ci:manual_test\n//ci:source.py\n//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir",
        )
        if impacted:
            print(impacted)


if __name__ == "__main__":
    main()
