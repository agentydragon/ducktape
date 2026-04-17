"""Minimal task engine for session hook refactor prototype.

Contract per task:
  - shell command, declared deps, timeout
  - `$ENV_OUT` file the task appends `export K=V` lines to
  - stdout/stderr captured per-task into in-memory buffers
  - engine sources transitive deps' ENV_OUT files into child env before run

Contract across engine:
  - failures never abort (session-always-starts semantics)
  - `drain()` returns unread output across all tasks + mailbox
  - `session_env_content()` = cat envs/*.env in task declaration order
"""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    TIMEOUT = "timeout"


@dataclass
class Task:
    name: str
    shell: str
    requires: list[str] = field(default_factory=list)
    timeout: float | None = None
    background: bool = False

    state: TaskState = TaskState.PENDING
    exit_code: int | None = None
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    env_out_path: Path | None = None

    _stdout_cursor: int = 0
    _stderr_cursor: int = 0


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse `export K=V` / `K=V` / quoted values. Tolerates empty + comments."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        # shlex handles quotes/escapes like bash would
        try:
            tokens = shlex.split(v)
            v_parsed = tokens[0] if tokens else ""
        except ValueError:
            v_parsed = v
        result[k] = v_parsed
    return result


class Engine:
    def __init__(self, *, session_dir: Path, base_env: dict[str, str] | None = None) -> None:
        self.session_dir = session_dir
        self.envs_dir = session_dir / "envs"
        self.envs_dir.mkdir(parents=True, exist_ok=True)
        self.base_env = dict(base_env if base_env is not None else os.environ)
        self.tasks: dict[str, Task] = {}
        self._order: list[str] = []  # declaration order; used for env file composition
        self.mailbox: list[str] = []
        self._mailbox_cursor: int = 0

    def add(self, task: Task) -> None:
        if task.name in self.tasks:
            raise ValueError(f"duplicate task: {task.name}")
        self.tasks[task.name] = task
        self._order.append(task.name)
        # Numeric prefix keeps cat *.env deterministic by declaration order
        idx = len(self._order)
        task.env_out_path = self.envs_dir / f"{idx:03d}-{task.name}.env"

    def post(self, message: str) -> None:
        self.mailbox.append(message)

    def _transitive_deps(self, name: str) -> list[str]:
        order: list[str] = []
        seen: set[str] = set()

        def visit(n: str) -> None:
            for d in self.tasks[n].requires:
                if d in seen:
                    continue
                seen.add(d)
                visit(d)
                order.append(d)

        visit(name)
        return order

    async def run(self) -> None:
        pending = set(self.tasks)
        running: dict[str, asyncio.Task[None]] = {}
        while pending or running:
            ready = [
                n
                for n in pending
                if all(self.tasks[d].state in (TaskState.DONE, TaskState.TIMEOUT) for d in self.tasks[n].requires)
            ]
            for n in ready:
                pending.remove(n)
                running[n] = asyncio.create_task(self._run_one(self.tasks[n]))
            if not running:
                # Unreachable deps (cycle or missing) — abandon the rest
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for n in [n for n, t in running.items() if t in done]:
                running.pop(n)

    async def _run_one(self, task: Task) -> None:
        task.state = TaskState.RUNNING
        env = dict(self.base_env)
        for dep in self._transitive_deps(task.name):
            env.update(_parse_env_file(self.tasks[dep].env_out_path))  # type: ignore[arg-type]
        assert task.env_out_path is not None
        env["ENV_OUT"] = str(task.env_out_path)
        env["SESSION_DIR"] = str(self.session_dir)
        env["TASK_NAME"] = task.name
        task.env_out_path.touch()

        proc = await asyncio.create_subprocess_shell(
            task.shell, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=Path.cwd()
        )

        async def drain(stream: asyncio.StreamReader, buf: list[str]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                buf.append(line.decode(errors="replace").rstrip("\n"))

        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            await asyncio.wait_for(
                asyncio.gather(drain(proc.stdout, task.stdout), drain(proc.stderr, task.stderr), proc.wait()),
                timeout=task.timeout,
            )
            task.exit_code = proc.returncode
            task.state = TaskState.DONE
        except TimeoutError:
            proc.kill()
            await proc.wait()
            task.state = TaskState.TIMEOUT

    def drain(self) -> str:
        """Emit unread output across all tasks + mailbox (advances cursors)."""
        lines: list[str] = []
        for name in self._order:
            t = self.tasks[name]
            lines.extend(f"[{name}] {line}" for line in t.stdout[t._stdout_cursor :])
            lines.extend(f"[{name}!] {line}" for line in t.stderr[t._stderr_cursor :])
            t._stdout_cursor = len(t.stdout)
            t._stderr_cursor = len(t.stderr)
            if t.state == TaskState.TIMEOUT:
                lines.append(f"[{name}] (timed out after {t.timeout}s)")
            elif t.state == TaskState.DONE and t.exit_code:
                lines.append(f"[{name}] (exit {t.exit_code})")
        lines.extend(f"[mailbox] {m}" for m in self.mailbox[self._mailbox_cursor :])
        self._mailbox_cursor = len(self.mailbox)
        return "\n".join(lines)

    def session_env_content(self) -> str:
        """Final session env file content — cat envs/*.env in declaration order."""
        parts: list[str] = []
        for name in self._order:
            t = self.tasks[name]
            assert t.env_out_path is not None
            content = t.env_out_path.read_text().strip()
            if content:
                parts.append(f"# --- {name} ---\n{content}")
        return "\n".join(parts) + "\n" if parts else ""
