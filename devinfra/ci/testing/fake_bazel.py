#!/bin/python
import json
import os
import re
import sys
from pathlib import Path


def main() -> None:
    log_dir = Path(os.environ["CI_TEST_LOG_DIR"])
    lockfile = Path.cwd() / "MODULE.bazel.lock"
    expected_lockfile = os.environ.get("FAKE_EXPECT_LOCKFILE")
    actual_lockfile = lockfile.read_text() if lockfile.exists() else None
    if expected_lockfile is not None and actual_lockfile != expected_lockfile:
        raise SystemExit(f"unexpected MODULE.bazel.lock contents: {actual_lockfile!r}")

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
            if labels:
                print("\n".join(sorted(labels)))
        elif query_arg.endswith("test-query.txt"):
            if os.environ.get("FAKE_SELECTION_SNAPSHOT_WRITE_FAIL") == "1":
                Path(query_arg.split("=", 1)[1]).with_name("selected-test-labels.json").mkdir()
            tests = os.environ.get("FAKE_TESTS", "//ci:normal_test")
            if tests:
                print(tests)
        elif query_arg.endswith("runnable-tests-query.txt"):
            (log_dir / "runnable-tests-query.log").write_text(query)
            tag_filter = re.search(r'attr\("tags", ("(?:[^"\\]|\\.)*"),', query)
            if tag_filter is None:
                raise SystemExit("CI tag exclusion missing")
            excluded = json.loads(tag_filter.group(1))
            tags = json.loads(os.environ.get("FAKE_TEST_TAGS", "{}"))
            runnable = [
                label for label in labels if not re.search(excluded, "[" + ", ".join(tags.get(label, [])) + "]")
            ]
            if runnable:
                print("\n".join(runnable))
        elif query_arg.endswith("contract-sources-query.txt"):
            print(os.environ.get("FAKE_CONTRACT_SOURCES", "//agentplane/env/runtime/journal:journal.go"))
        elif query_arg.endswith("contract-owners-query.txt"):
            print(os.environ.get("FAKE_CONTRACT_OWNERS", "//agentplane/env/runtime/journal:journal"))
        elif query_arg.endswith("contract-required-query.txt"):
            tags = json.loads(os.environ.get("FAKE_TEST_TAGS", "{}"))
            print("\n".join(label for label in labels if not {"manual", "live_openai_api"} & set(tags.get(label, []))))
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
                (log_dir / "test-targets.log").write_text(Path(target_file).read_text())
        else:
            (log_dir / "build-args.log").write_text("\n".join(args))
            target_file = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--target_pattern_file=")), None)
            if target_file:
                (log_dir / "affected-targets.log").write_text(Path(target_file).read_text())
        generated_lockfile = os.environ.get("FAKE_GENERATE_LOCKFILE")
        if generated_lockfile:
            lockfile.write_text(generated_lockfile)
        raise SystemExit(int(os.environ.get(f"FAKE_{role.upper()}_EXIT", "0")))
    else:
        raise SystemExit(f"unexpected bazel args: {args}")


if __name__ == "__main__":
    main()
