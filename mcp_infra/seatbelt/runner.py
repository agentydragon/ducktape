"""
Seatbelt runner: execute commands under an SBPLPolicy with async subprocess-like APIs.
- No magic rule injection. The policy you pass is the policy used.
- Trace paths are managed internally when trace=True (on a deep copy of the policy).
- Outputs are bytes (no implicit decoding). Durations are datetime.timedelta.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp_infra.seatbelt.compile import compile_sbpl
from mcp_infra.seatbelt.model import SBPLPolicy, TraceConfig


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    stdout: bytes | None
    stderr: bytes | None
    cmd: list[str]
    sandbox_exec_path: str
    started_at: datetime
    ended_at: datetime
    duration: timedelta
    policy_path: Path
    policy_text: str
    trace_path: Path | None
    trace_text: str | None
    unified_sandbox_denies_path: Path | None = None
    unified_sandbox_denies_text: str | None = None


def _prepare_policy(
    policy: SBPLPolicy, *, artifacts_dir: Path, trace: bool, trace_dir: Path | None
) -> tuple[SBPLPolicy, Path, Path | None, str]:
    """Write policy.sb and return (policy_copy_or_original, policy_file, trace_file, policy_text)."""
    policy_file = artifacts_dir / "policy.sb"
    sp = policy
    trace_file: Path | None = None
    if trace:
        # If caller did not set trace.path, set one on a deep copy
        if not policy.trace.enabled or not policy.trace.path:
            trace_file = (trace_dir or artifacts_dir) / "seatbelt.trace.log"
            sp = policy.model_copy(deep=True)
            sp.trace = TraceConfig(enabled=True, path=str(trace_file))
        else:
            trace_file = Path(policy.trace.path)
    policy_text = compile_sbpl(sp)
    policy_file.write_text(policy_text)
    return sp, policy_file, trace_file, policy_text


def collect_unified_sandbox_denies(artifacts_dir: Path, window: str = "5m") -> tuple[Path | None, str | None]:
    """Collect recent unified log deny messages from the macOS sandbox.

    Returns (path, text) where path is the written file under artifacts_dir and
    text is the full log output. Returns (None, None) on non-darwin.
    """
    if sys.platform != "darwin":
        return None, None
    cmd = [
        "/usr/bin/log",
        "show",
        "--style",
        "syslog",
        "--info",
        "--debug",
        "--last",
        window,
        "--predicate",
        '((subsystem == "com.apple.sandbox") OR (category == "Sandbox"))',
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"log show failed rc={res.returncode}: {res.stderr!r}")
    text = res.stdout or ""
    dest = artifacts_dir / "unified_sandbox_deny.log"
    dest.write_text(text)
    return dest, text


def _collect_unified_if_failed(artifacts_dir: Path, returncode: int | None) -> tuple[Path | None, str | None]:
    """Collect unified sandbox denies only when exit code is non-zero."""
    if returncode not in (None, 0):
        return collect_unified_sandbox_denies(artifacts_dir)
    return None, None


def _finalize_result(
    *,
    returncode: int | None,
    stdout: bytes | None,
    stderr: bytes | None,
    started: datetime,
    ended: datetime,
    policy_file: Path,
    policy_text: str,
    trace_file: Path | None,
    read_trace: bool,
    keep_files: bool,
    artifacts_dir: Path,
    cmd: list[str],
    sandbox_exec_path: str,
) -> RunResult:
    """Common tail: read trace, collect unified denies, build RunResult, cleanup."""
    duration_td = ended - started

    trace_text: str | None = None
    if read_trace and trace_file and trace_file.exists():
        trace_text = trace_file.read_text(errors="replace")

    if not keep_files:
        # Keep trace by default; only remove policy file
        policy_file.unlink()

    unified_path, unified_text = _collect_unified_if_failed(artifacts_dir, returncode)

    return RunResult(
        exit_code=(returncode if returncode is not None else -1),
        stdout=stdout,
        stderr=stderr,
        cmd=cmd,
        sandbox_exec_path=sandbox_exec_path,
        started_at=started,
        ended_at=ended,
        duration=duration_td,
        policy_path=policy_file,
        policy_text=policy_text,
        trace_path=trace_file,
        trace_text=trace_text,
        unified_sandbox_denies_path=unified_path,
        unified_sandbox_denies_text=unified_text,
    )


# NOTE: The synchronous runner has been removed.
# Seatbelt now provides async-only APIs (run_sandboxed_async, apopen).


async def run_sandboxed_async(
    policy: SBPLPolicy,
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    trace: bool = False,
    trace_dir: Path | None = None,
    read_trace: bool = True,
    keep_files: bool | None = None,
    sandbox_exec: str | None = None,
    stdin: int | None = asyncio.subprocess.PIPE,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
) -> RunResult:
    """Async one-shot runner. If stdout/stderr are PIPE, uses communicate(); otherwise waits."""
    sx = sandbox_exec or shutil.which("sandbox-exec")
    if not sx:
        raise FileNotFoundError("sandbox-exec not found on PATH; macOS-only")

    artifacts_dir = Path(tempfile.mkdtemp(prefix="seatbelt-run-"))
    _policy, policy_file, trace_file, policy_text = _prepare_policy(
        policy, artifacts_dir=artifacts_dir, trace=trace, trace_dir=trace_dir
    )

    cmd = [sx, "-f", str(policy_file), *argv]
    started = datetime.now(UTC)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout, stderr=stderr
    )
    if asyncio.subprocess.PIPE in {stdout, stderr}:
        out_b, err_b = await proc.communicate()
    else:
        await proc.wait()
        out_b, err_b = None, None
    ended = datetime.now(UTC)
    keep = trace if keep_files is None else keep_files
    return _finalize_result(
        returncode=proc.returncode,
        stdout=out_b,
        stderr=err_b,
        started=started,
        ended=ended,
        policy_file=policy_file,
        policy_text=policy_text,
        trace_file=trace_file,
        read_trace=read_trace,
        keep_files=keep,
        artifacts_dir=artifacts_dir,
        cmd=cmd,
        sandbox_exec_path=sx,
    )


class AsyncSeatbeltPopen:
    """Async Popen-like handle for sandboxed processes.

    Exposes stdin (StreamWriter), stdout/stderr (StreamReader), and lifecycle methods.
    Use via apopen(...), or as an async context manager to autocleanup artifacts.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        artifacts_dir: Path,
        policy_file: Path,
        policy_text: str,
        trace_file: Path | None,
        sandbox_exec_path: str,
        cmd: list[str],
    ) -> None:
        self._proc = proc
        self._artifacts_dir = artifacts_dir
        self._policy_file = policy_file
        self._policy_text = policy_text
        self._trace_file = trace_file
        self.sandbox_exec_path = sandbox_exec_path
        self.cmd = cmd

    # Streams
    @property
    def stdin(self):
        return self._proc.stdin

    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stderr(self):
        return self._proc.stderr

    # Process info
    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    # Artifacts and policy
    @property
    def policy_file(self) -> Path:
        return self._policy_file

    @property
    def policy_text(self) -> str:
        return self._policy_text

    @property
    def trace_file(self) -> Path | None:
        return self._trace_file

    @property
    def artifacts_dir(self) -> Path:
        return self._artifacts_dir

    # Lifecycle
    async def wait(self) -> int:
        return await self._proc.wait()

    async def communicate(self, input: bytes | None = None):
        """Communicate with process. Use asyncio.timeout() around this call to add a timeout."""
        return await self._proc.communicate(input)

    def send_signal(self, sig: int) -> None:
        self._proc.send_signal(sig)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def cleanup(self) -> None:
        shutil.rmtree(self._artifacts_dir, ignore_errors=False)

    async def __aenter__(self) -> AsyncSeatbeltPopen:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._proc.returncode is None:
                # Two-phase termination policy (deterministic):
                # 1) terminate and wait up to grace seconds
                # 2) kill and wait unbounded (shielded) if still alive
                grace = float(os.getenv("ADGN_SEATBELT_TERM_GRACE_SECS", "2.0"))
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=grace)
                except TimeoutError:
                    self._proc.kill()
                    # Ensure we wait for process exit regardless of outer cancellations
                    await asyncio.shield(self._proc.wait())
        finally:
            self.cleanup()


async def apopen(
    args: list[str] | tuple[str, ...],
    policy: SBPLPolicy,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    trace: bool = False,
    stdin: int | None = asyncio.subprocess.PIPE,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
) -> AsyncSeatbeltPopen:
    """Async Popen-like launcher for sandboxed processes.

    Returns an AsyncSeatbeltPopen with interactive streams (if PIPE) and managed artifacts.
    """
    sx = shutil.which("sandbox-exec")
    if not sx:
        raise FileNotFoundError("sandbox-exec not found on PATH; macOS-only")

    artifacts_dir = Path(tempfile.mkdtemp(prefix="seatbelt-apopen-"))
    # Configure trace path on a deep copy if requested and write policy
    _sp, policy_file, trace_file, policy_text = _prepare_policy(
        policy, artifacts_dir=artifacts_dir, trace=trace, trace_dir=None
    )

    cmd = [sx, "-f", str(policy_file), *list(args)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout, stderr=stderr
    )
    return AsyncSeatbeltPopen(
        proc,
        artifacts_dir=artifacts_dir,
        policy_file=policy_file,
        policy_text=policy_text,
        trace_file=trace_file,
        sandbox_exec_path=sx,
        cmd=cmd,
    )
