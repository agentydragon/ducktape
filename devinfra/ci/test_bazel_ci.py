import os
import subprocess
from pathlib import Path

import pytest_bazel

REPO_ROOT = Path(__file__).resolve().parents[2]
BAZEL_CI = REPO_ROOT / "devinfra/ci/bazel_ci.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_pr_affected_targets_match_wildcard_semantics(tmp_path: Path) -> None:
    """Manual and source-file labels are removed without losing quoted '+' labels."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    query_log = tmp_path / "query.log"
    test_args_log = tmp_path / "test-args.log"

    _write_executable(
        bin_dir / "git",
        """#!/bin/python
import sys

args = sys.argv[1:]
if args[:2] == ["rev-parse", "HEAD"]:
    print("merge")
elif args[:2] == ["rev-parse", "merge^1"]:
    print("base")
elif args[:2] == ["rev-parse", "merge^2"]:
    print("pr-head")
elif args and (args[0] == "fetch" or "checkout" in args):
    pass
else:
    raise SystemExit(f"unexpected git args: {args}")
""",
    )
    _write_executable(
        bin_dir / "bazel-diff",
        """#!/bin/python
import sys

if sys.argv[1] == "get-impacted-targets":
    print("//ci:normal_test")
    print("//ci:manual_test")
    print("//ci:source.py")
    print("//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir")
""",
    )
    _write_executable(bin_dir / "python3", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "bazel",
        f"""#!/bin/python
import re
import sys
from pathlib import Path

args = sys.argv[1:]
query_arg = next((arg for arg in args if arg.startswith("--query_file=")), None)
if query_arg:
    query = Path(query_arg.split("=", 1)[1]).read_text()
    labels = list(dict.fromkeys(re.findall(r'\"(//[^\"]+)\"', query)))
    if query_arg.endswith("affected-query.txt"):
        Path({str(query_log)!r}).write_text(query)
        if 'except attr("tags", "manual", set(' not in query:
            raise SystemExit("manual exclusion missing")
        labels = [
            label
            for label in labels
            if not label.endswith(":manual_test") and not label.endswith(":source.py")
        ]
        print("\\n".join(sorted(labels)))
    elif query_arg.endswith("test-query.txt"):
        print("//ci:normal_test")
    else:
        raise SystemExit(f"unexpected query file: {{query_arg}}")
elif args and args[0] == "shutdown":
    pass
elif args and args[0] in {"test", "build"}:
    if args[0] == "test":
        Path({str(test_args_log)!r}).write_text("\\n".join(args))
else:
    raise SystemExit(f"unexpected bazel args: {{args}}")
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GITHUB_EVENT_NAME": "pull_request",
            "PR_HEAD_SHA": "pr-head",
            "PR_BASE_SHA": "base",
            "RBE_IMAGE": "test-image",
            "TEST_INVOCATION_ID": "11111111-1111-1111-1111-111111111111",
            "BUILD_INVOCATION_ID": "22222222-2222-2222-2222-222222222222",
        }
    )
    result = subprocess.run(
        ["bash", str(BAZEL_CI)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert Path("/tmp/affected.txt").read_text() == (
        "//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir\n//ci:normal_test\n"
    )
    # The pre-assigned ID has to reach Bazel: it is the only handle a consumer has on
    # this invocation when the run is cancelled before `bb remote` returns.
    assert "--invocation_id=11111111-1111-1111-1111-111111111111" in test_args_log.read_text().splitlines()
    query = query_log.read_text()
    assert 'except kind("source file", set(' in query
    assert 'except attr("tags", "manual", set(' in query
    assert '"//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir"' in query


if __name__ == "__main__":
    pytest_bazel.main()
