"""SWE-bench task wrapper that swaps the default `bash_session` (TTY-typing)
solver for `swe_bench_react_agent` (stateless `bash` + `python` + `think`).

The canonical `inspect_evals.swe_bench.swe_bench` task hardcodes
`solver=swe_bench_agent_with_inspect_tool_support(...)` and exposes no
solver param. `gpt-oss:20b` was getting confused by `bash_session`'s
`type` / `type_submit` distinction (e.g. issuing `action: "type"`
without a follow-up submit, leaving the shell waiting on input). The
react agent's prompt is explicit that "Your bash session is NOT
stateful, so all commands must be self-contained" and the underlying
tool just runs a command and returns output.

Pass through the canonical task's params so we still benefit from
upstream changes; swap only the solver and (optionally) the message
limit.
"""

from __future__ import annotations

from inspect_ai import Task, task, task_with
from inspect_evals.swe_bench.swe_bench import (
    DEFAULT_TOOL_TIMEOUT,
    swe_bench as canonical_swe_bench,
    swe_bench_react_agent,
)


@task
def swe_bench_react(
    tool_timeout: int = DEFAULT_TOOL_TIMEOUT, message_limit: int | None = None, **kwargs: object
) -> Task:
    base = canonical_swe_bench(tool_timeout=tool_timeout, **kwargs)
    overrides: dict[str, object] = {"solver": swe_bench_react_agent(tool_timeout=tool_timeout)}
    if message_limit is not None:
        overrides["message_limit"] = message_limit
    return task_with(base, **overrides)
