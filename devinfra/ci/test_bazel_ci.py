import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

REPO_ROOT = Path(__file__).resolve().parents[2]
BAZEL_CI = REPO_ROOT / "devinfra/ci/bazel_ci.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("changed_file", "graph_wide"),
    [
        ("devinfra/ci/test_bazel_ci.py", False),
        ("MODULE.bazel", True),
        ("third_party/cli_proxy_api/go.mod", True),
        ("third_party/activitywatch/package.json", True),
        ("pnpm-lock.yaml", True),
        ("Cargo.toml", True),
        ("pyproject.toml", True),
        ("devinfra/ci/example.bzl", True),
    ],
)
def test_pr_target_selection(tmp_path: Path, changed_file: str, graph_wide: bool) -> None:
    """Graph metadata uses //..., while ordinary changes retain bazel-diff filtering."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bazel_diff_args_log = tmp_path / "bazel-diff-args.log"
    query_log = tmp_path / "query.log"
    test_args_log = tmp_path / "test-args.log"

    _write_executable(
        bin_dir / "git",
        """#!/bin/python
import os
import sys

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
    pass
else:
    raise SystemExit(f"unexpected git args: {args}")
""",
    )
    _write_executable(
        bin_dir / "bazel-diff",
        f"""#!/bin/python
import sys
from pathlib import Path

if sys.argv[1] == "get-impacted-targets":
    Path({str(bazel_diff_args_log)!r}).write_text("\\n".join(sys.argv[1:]))
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
            "CHANGED_FILE": changed_file,
            "RBE_IMAGE": "test-image",
            "TEST_INVOCATION_ID": "11111111-1111-1111-1111-111111111111",
            "BUILD_INVOCATION_ID": "22222222-2222-2222-2222-222222222222",
        }
    )
    result = subprocess.run(
        ["bash", str(BAZEL_CI)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr + result.stdout
    test_args = test_args_log.read_text().splitlines()
    if graph_wide:
        assert f"graph-wide change: {changed_file}" in result.stdout
        assert not bazel_diff_args_log.exists()
        assert "//..." in test_args
        assert "--target_pattern_file=/tmp/affected.txt" not in test_args
    else:
        assert Path("/tmp/affected.txt").read_text() == (
            "//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir\n//ci:normal_test\n"
        )
        bazel_diff_args = bazel_diff_args_log.read_text().splitlines()
        assert bazel_diff_args[:3] == ["get-impacted-targets", "-w", str(REPO_ROOT)]
        assert "--target_pattern_file=/tmp/affected.txt" in test_args
        query = query_log.read_text()
        assert 'except kind("source file", set(' in query
        assert 'except attr("tags", "manual", set(' in query
        assert '"//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir"' in query

    # The pre-assigned ID has to reach Bazel: it is the only handle a consumer has on
    # this invocation when the run is cancelled before `bb remote` returns.
    assert "--invocation_id=11111111-1111-1111-1111-111111111111" in test_args


if __name__ == "__main__":
    pytest_bazel.main()
