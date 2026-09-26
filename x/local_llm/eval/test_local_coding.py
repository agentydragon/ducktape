"""Optional small coding-readiness smoke eval against fixed tasks in disposable Sandboxes."""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import pytest
import pytest_bazel

from agentplane.acceptance.agent import Agent
from agentplane.app.client import Client
from agentplane.app.inventory import SandboxView
from agentplane.app.presets import Harness
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/acceptance:agent
# gazelle:include_dep //agentplane/acceptance:conftest

# Explicitly reuse the deployed Agentplane client and disposable-Sandbox lifecycle outside its
# acceptance package; this smoke eval remains owned by x/local_llm.
pytest_plugins = ("agentplane.acceptance.conftest",)

LOCAL_CODING_ROUTE = "llama-cpp/oai-chat/qwen3.8-27b-q8"
MODEL_ENV = "LOCAL_LLM_CODING_MODEL"
CASE_ENV = "LOCAL_LLM_CODING_CASE"
NAMESPACE_ENV = "AGENTPLANE_ACCEPTANCE_NAMESPACE"
DEFAULT_NAMESPACE = "agentplane-testing"

# At roughly 50 generated tokens/second, this allows about 30k tokens for one small task,
# including tool-use turns to inspect failures and repair the implementation. The full matrix
# needs four Sandboxes; its Bazel timeout is eternal because each can also spend five minutes
# becoming ready.
CODING_TURN_SECONDS = 600.0
WORK = PurePosixPath("/state/work")


@dataclass(frozen=True)
class CodingTask:
    name: str
    module: str
    baseline: str
    tests: str
    prompt: str

    @property
    def files(self) -> dict[str, str]:
        return {self.module: self.baseline, "tests/__init__.py": "", "tests/test_task.py": self.tests}


TOPOLOGICAL_ORDER = CodingTask(
    name="stable-topological-order",
    module="stable_order.py",
    baseline='''from collections.abc import Iterable


def stable_topological_order(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    """Return a stable topological order; edges are (prerequisite, dependent)."""
    raise NotImplementedError
''',
    tests="""import unittest

from stable_order import stable_topological_order


class StableTopologicalOrderTests(unittest.TestCase):
    def test_preserves_original_order_among_all_ready_nodes(self) -> None:
        self.assertEqual(
            stable_topological_order(["a", "b", "c", "d"], [("a", "d"), ("b", "c")]),
            ["a", "b", "c", "d"],
        )

    def test_deduplicates_repeated_nodes_and_edges(self) -> None:
        self.assertEqual(
            stable_topological_order(
                ["fetch", "compile", "lint", "package", "fetch"],
                [("fetch", "compile"), ("compile", "package"), ("lint", "package"), ("fetch", "compile")],
            ),
            ["fetch", "compile", "lint", "package"],
        )

    def test_rejects_unknown_edge_endpoints(self) -> None:
        for edge in [("missing", "a"), ("a", "missing")]:
            with self.subTest(edge=edge), self.assertRaises(ValueError):
                stable_topological_order(["a"], [edge])

    def test_rejects_cycles(self) -> None:
        with self.assertRaises(ValueError):
            stable_topological_order(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])


if __name__ == "__main__":
    unittest.main()
""",
    prompt="""Implement `stable_topological_order(nodes, edges)` in `stable_order.py` using only the Python standard library.

Requirements:
- `edges` contains `(prerequisite, dependent)` pairs.
- Return a topological ordering. Among nodes that are ready at the same time, always choose the node that appeared earliest in the first-occurrence order of `nodes` (including when a newly-ready node competes with an older ready node).
- Ignore repeated node names after their first occurrence, and ignore repeated edges.
- Raise `ValueError` if either endpoint of an edge is unknown or if the graph has a cycle.
- Keep the implementation focused and typed. Do not modify the tests.

Run `python3 -m unittest -v tests.test_task`, fix any implementation failures, and report briefly what you changed.""",
)

JSONL_TRANSFORM = CodingTask(
    name="jsonl-transform",
    module="jsonl_transform.py",
    baseline='''def transform_jsonl(text: str) -> str:
    """Normalize JSON Lines records as described in the acceptance task."""
    raise NotImplementedError
''',
    tests="""import unittest

from jsonl_transform import transform_jsonl


class JsonlTransformTests(unittest.TestCase):
    def test_normalizes_records_and_preserves_unicode(self) -> None:
        source = (
            '\\n{"count": 2, "name": " Jose\\u0301 ", "tags": [" bleu ", "bleu", "", "東京"]}'
            '\\n{"name": "Zoe 🚀"}\\n'
        )
        expected = (
            '{"count":2,"name":"José","tags":["bleu","東京"]}\\n'
            '{"name":"Zoe 🚀","tags":[]}\\n'
        )
        self.assertEqual(transform_jsonl(source), expected)

    def test_reports_malformed_json_line(self) -> None:
        with self.assertRaisesRegex(ValueError, "line 3"):
            transform_jsonl('{"name":"ok"}\\n\\n{"name":')

    def test_rejects_non_objects_and_invalid_names(self) -> None:
        for source in ['[]', '{"name": "  "}', '{"name": 4}']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                transform_jsonl(source)

    def test_rejects_non_string_tags(self) -> None:
        for tags in ['"blue"', '["blue", 3]']:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                transform_jsonl('{"name":"ok","tags":' + tags + '}')


if __name__ == "__main__":
    unittest.main()
""",
    prompt="""Implement `transform_jsonl(text)` in `jsonl_transform.py` using only the Python standard library.

The function receives JSON Lines text and returns normalized JSON Lines text:
- Ignore blank or whitespace-only input lines.
- Each other line must decode to an object with a non-empty string `name` and an optional `tags` list containing only strings. Raise `ValueError` for malformed JSON or invalid fields, and include the 1-based input line number in the error.
- Normalize `name` and each tag to Unicode NFC, then strip surrounding whitespace. Reject an empty normalized name. Drop empty tags and deduplicate tags while preserving their first normalized occurrence. An omitted `tags` field becomes an empty list.
- Preserve other object fields. Emit compact JSON with sorted keys and literal Unicode (do not escape non-ASCII characters), one record per line, ending each output record with a newline.
- Do not modify the tests.

Run `python3 -m unittest -v tests.test_task`, fix any implementation failures, and report briefly what you changed.""",
)

TASKS = (TOPOLOGICAL_ORDER, JSONL_TRANSFORM)
CASES = [(harness, task) for harness in (protocol_pb2.HARNESS_CODEX, protocol_pb2.HARNESS_CLAUDE) for task in TASKS]


def _case_id(case: tuple[protocol_pb2.Harness, CodingTask]) -> str:
    harness, task = case
    return f"{protocol_pb2.Harness.Name(harness).lower()}-{task.name}"


_selected_case = os.environ.get(CASE_ENV)
if _selected_case:
    CASES = [case for case in CASES if _case_id(case) == _selected_case]
    if not CASES:
        raise ValueError(f"unknown {CASE_ENV}: {_selected_case}")


def _bootstrap(task: CodingTask) -> str:
    """Create a tiny committed task repository; fixed tests are restored after the turn."""
    lines = ["set -eu", f"mkdir -p {shlex.quote(str(WORK / 'tests'))}"]
    for relative, contents in task.files.items():
        path = WORK / relative
        lines.extend(
            [
                f"mkdir -p {shlex.quote(str(path.parent))}",
                f"printf %s {shlex.quote(contents)} > {shlex.quote(str(path))}",
            ]
        )
    lines.extend(
        [
            f"cd {shlex.quote(str(WORK))}",
            "git init -q",
            "git config user.name 'Agentplane Acceptance'",
            "git config user.email 'agentplane-acceptance@example.invalid'",
            "git add .",
            "git commit -qm 'Seed local coding acceptance task'",
        ]
    )
    return "\n".join(lines)


def _kubectl_exec(sandbox: SandboxView, *arguments: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    namespace = os.environ.get(NAMESPACE_ENV, DEFAULT_NAMESPACE)
    command = ["kubectl", "-n", namespace, "exec"]
    command.extend([sandbox.name, "-c", "runner", "--", *arguments])
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)


def _write_remote_file(sandbox: SandboxView, path: PurePosixPath, contents: str) -> None:
    """Replace a file from the trusted test controller, passing its bytes as base64 argv."""
    encoded = base64.b64encode(contents.encode("utf-8")).decode("ascii")
    writer = "import base64,pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]))"
    result = _kubectl_exec(sandbox, "python3", "-I", "-c", writer, str(path), encoded)
    if result.returncode != 0:
        raise RuntimeError(f"controller could not restore {path}: {result.stderr[-2000:]}")


def _read_remote_file(sandbox: SandboxView, path: PurePosixPath) -> bytes:
    reader = (
        "import base64,pathlib,sys; print(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode('ascii'))"
    )
    result = _kubectl_exec(sandbox, "python3", "-I", "-c", reader, str(path))
    if result.returncode != 0:
        raise RuntimeError(f"controller could not read {path}: {result.stderr[-2000:]}")
    return base64.b64decode(result.stdout.strip(), validate=True)


def _restore_and_verify_tests(sandbox: SandboxView, task: CodingTask) -> bool:
    for relative in ("tests/__init__.py", "tests/test_task.py"):
        _write_remote_file(sandbox, WORK / relative, task.files[relative])
    return all(
        _read_remote_file(sandbox, WORK / relative) == task.files[relative].encode("utf-8")
        for relative in ("tests/__init__.py", "tests/test_task.py")
    )


def _agent_left_tests_unchanged(sandbox: SandboxView, task: CodingTask) -> bool:
    try:
        return all(
            _read_remote_file(sandbox, WORK / relative) == task.files[relative].encode("utf-8")
            for relative in ("tests/__init__.py", "tests/test_task.py")
        )
    except RuntimeError, ValueError:
        return False


def _controller_test(sandbox: SandboxView, task: CodingTask) -> subprocess.CompletedProcess[str]:
    """Run the controller's fixed unittest source, isolated from files the agent could add."""
    runner = (
        "import base64,sys,types,unittest; "
        "sys.path.append('/state/work'); "
        "module=types.ModuleType('_fixed_acceptance_tests'); "
        "source=base64.b64decode(sys.argv[1]).decode('utf-8'); "
        "exec(compile(source, '<fixed acceptance tests>', 'exec'), module.__dict__); "
        "suite=unittest.defaultTestLoader.loadTestsFromModule(module); "
        "result=unittest.TextTestRunner(stream=sys.stdout,verbosity=2).run(suite); "
        "sys.exit(not result.wasSuccessful())"
    )
    encoded = base64.b64encode(task.tests.encode("utf-8")).decode("ascii")
    return _kubectl_exec(sandbox, "python3", "-I", "-c", runner, encoded, timeout=120.0)


def _workspace_git_snapshot(sandbox: SandboxView, baseline_commit: str) -> dict[str, object]:
    git = ["git", "-C", str(WORK)]
    status = _kubectl_exec(sandbox, *git, "status", "--short", "--untracked-files=all", "--ignored")
    diff = _kubectl_exec(
        sandbox,
        *git,
        "-c",
        "diff.external=",
        "-c",
        "core.pager=cat",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        baseline_commit,
        "--",
    )
    return {
        "status_returncode": status.returncode,
        "status": status.stdout,
        "status_error": status.stderr,
        "diff_returncode": diff.returncode,
        "diff_from_seed": diff.stdout,
        "diff_error": diff.stderr,
    }


def _source_diff(task: CodingTask, sandbox: SandboxView) -> str:
    original = task.baseline.splitlines(keepends=True)
    changed = _read_remote_file(sandbox, WORK / task.module).decode("utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(original, changed, fromfile=f"a/{task.module}", tofile=f"b/{task.module}"))


@pytest.mark.parametrize(("harness", "task"), CASES, ids=[_case_id(case) for case in CASES])
async def test_local_model_coding(
    client: Client, sandbox: Callable[..., Awaitable[SandboxView]], harness: protocol_pb2.Harness, task: CodingTask
) -> None:
    case_id = _case_id((harness, task))
    evidence: dict[str, object] = {
        "case": case_id,
        "route": os.environ.get(MODEL_ENV, LOCAL_CODING_ROUTE),
        "status": "started",
        "turn_timeout_seconds": CODING_TURN_SECONDS,
    }
    try:
        model = os.environ.get(MODEL_ENV, LOCAL_CODING_ROUTE)
        offered = (await client.models())[Harness(protocol_pb2.Harness.Name(harness))]
        assert model in offered, f"route {model!r} absent from offered models for {case_id}"
        harness_slug = protocol_pb2.Harness.Name(harness).removeprefix("HARNESS_").lower()
        view = await sandbox(f"accept-code-{task.name[:20]}-{harness_slug}", bootstrap=_bootstrap(task))
        evidence["sandbox"] = view.name

        agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
        evidence["thread_id"] = str(agent.thread_id)
        baseline = _kubectl_exec(view, "git", "-C", str(WORK), "rev-parse", "HEAD")
        assert baseline.returncode == 0, f"could not read seeded repository commit: {baseline.stderr[-2000:]}"
        baseline_commit = baseline.stdout.strip()
        evidence["seed_commit"] = baseline_commit
        async with asyncio.timeout(CODING_TURN_SECONDS):
            turn = await agent.run(task.prompt, read_seconds=CODING_TURN_SECONDS)
        evidence["turn_status"] = event_pb2.TurnStatus.Name(turn.status) if turn.status is not None else None
        evidence["tool_outputs"] = [output[:2000] for output in turn.tool_outputs]
        evidence["answer"] = turn.answer[:2000]
        evidence["agent_ran_passing_unittests"] = any(
            re.search(r"Ran\s+\d+\s+tests?", output) and re.search(r"\bOK\b", output) for output in turn.tool_outputs
        )

        agent_left_tests_unchanged = _agent_left_tests_unchanged(view, task)
        evidence["agent_left_fixed_tests_unchanged"] = agent_left_tests_unchanged
        tests_intact = _restore_and_verify_tests(view, task)
        evidence["canonical_tests_restored_and_verified"] = tests_intact
        if tests_intact:
            fixed_tests = _controller_test(view, task)
            evidence["controller_tests"] = {
                "returncode": fixed_tests.returncode,
                "stdout": fixed_tests.stdout[-8000:],
                "stderr": fixed_tests.stderr[-2000:],
            }
        else:
            fixed_tests = None
            evidence["controller_tests"] = {"status": "not-run; canonical test bytes did not match"}

        diff = _source_diff(task, view)
        evidence["implementation_diff"] = diff
        git_snapshot = _workspace_git_snapshot(view, baseline_commit)
        evidence["workspace_git"] = git_snapshot
        evidence["status"] = "passed"
        assert evidence["agent_ran_passing_unittests"], "agent did not run the requested unittest suite to success"
        assert agent_left_tests_unchanged, "agent changed or removed the fixed task tests during its turn"
        assert tests_intact, "controller could not restore and verify the canonical fixed tests"
        assert git_snapshot["status_returncode"] == 0, "controller could not capture the workspace git status"
        assert git_snapshot["diff_returncode"] == 0, "controller could not capture the workspace git diff"
        assert fixed_tests is not None, "controller did not run the fixed tests"
        assert fixed_tests.returncode == 0, (
            "controller-run fixed tests failed:\n" + fixed_tests.stdout + fixed_tests.stderr
        )
        assert diff, "agent did not change the implementation file"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error_type"] = type(error).__name__
        evidence["error"] = str(error)[:2000]
        raise
    finally:
        (undeclared_outputs_dir() / f"local-coding-{case_id}.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False)
        )


if __name__ == "__main__":
    pytest_bazel.main()
