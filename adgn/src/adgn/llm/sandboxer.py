import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

from adgn.seatbelt.compile import compile_sbpl
from adgn.seatbelt.model import (
    DefaultBehavior,
    FileOp,
    FileRule,
    LiteralFilter,
    MachLookupRule,
    NetworkOp,
    NetworkRule,
    ProcessRule,
    SBPLPolicy,
    Subpath,
    SystemRule,
    TraceConfig,
)

# -----------------------------
# Pydantic models for policy
# -----------------------------


class EnvConfig(BaseModel):
    set: dict[str, str] = Field(default_factory=dict)
    passthrough: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class FSConfig(BaseModel):
    read_paths: list[Path] = Field(default_factory=list)
    write_paths: list[Path] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class SeatbeltDevConfig(BaseModel):
    allow_tty_writes: bool | None = None
    model_config = ConfigDict(extra="forbid")


class SeatbeltExtraAllow(BaseModel):
    mach_lookup: list[str] = Field(default_factory=list)
    system_socket: bool | None = None
    sysctl_read: bool | None = None
    dev: SeatbeltDevConfig = Field(default_factory=SeatbeltDevConfig)
    file_read_extra: list[Path] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class SeatbeltPlatform(BaseModel):
    trace: bool = False
    extra_allow: SeatbeltExtraAllow = Field(default_factory=SeatbeltExtraAllow)
    model_config = ConfigDict(extra="forbid")


class PlatformConfig(BaseModel):
    # Platform-neutral tracing toggle; backends may map to their own tracing
    trace: bool = False
    seatbelt: SeatbeltPlatform = Field(default_factory=SeatbeltPlatform)
    model_config = ConfigDict(extra="forbid")


class NetProxyConfig(BaseModel):
    listen: str | None = None  # e.g., 127.0.0.1:0
    upstream: str | None = None  # e.g., host:port
    model_config = ConfigDict(extra="forbid")


class NetConfig(BaseModel):
    mode: Literal["none", "loopback", "open"] = "loopback"
    allow_domains: list[str] = Field(default_factory=list)
    proxy: NetProxyConfig | None = None
    model_config = ConfigDict(extra="forbid")


class Policy(BaseModel):
    env: EnvConfig = Field(default_factory=EnvConfig)
    fs: FSConfig = Field(default_factory=FSConfig)
    net: NetConfig = Field(default_factory=NetConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    model_config = ConfigDict(extra="forbid")


# -----------------------------
# SBPL adapter via adgn.seatbelt
# -----------------------------


def _abs(p: str | Path) -> Path:
    return Path(p).expanduser().absolute()


def _compose_sbpl(policy: Policy, trace_path: str | None) -> str:
    """Translate sandboxer.Policy -> SBPLPolicy and compile to SBPL text."""
    sp = SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(
            allow_process_star=True,
            allow_signal_self=True,
            allow_process_exec=True,  # allow exec of binaries under allowed FS roots
            allow_process_fork=True,  # allow fork for pipelines/shell
            allow_process_info_same_sandbox=True,  # allow process-info* for same sandbox
        ),
        system=SystemRule(
            system_socket=bool(policy.platform.seatbelt.extra_allow.system_socket),
            sysctl_read=bool(policy.platform.seatbelt.extra_allow.sysctl_read),
            user_preference_read=True,
        ),
        mach=MachLookupRule(global_names=list(policy.platform.seatbelt.extra_allow.mach_lookup)),
        trace=TraceConfig(enabled=bool(trace_path or policy.platform.seatbelt.trace), path=trace_path),
    )

    # Device basics
    dev_read_literals = ["/dev/null", "/dev/urandom", "/dev/random"]
    sp.files.append(FileRule(op=FileOp.FILE_READ_STAR, filters=[LiteralFilter(literal=p) for p in dev_read_literals]))
    sp.files.append(FileRule(op=FileOp.FILE_WRITE_STAR, filters=[LiteralFilter(literal="/dev/null")]))
    sp.files.append(FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/dev/tty")]))
    sp.files.append(FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/dev/tty")]))

    # Exec mapping and dyld/system roots
    sp.files.append(FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]))
    for root in (
        "/System",
        "/usr/lib",
        "/private/var/db/dyld",
        "/System/Volumes/Preboot",
        "/System/Cryptexes",
        "/System/Volumes/Preboot/Cryptexes",
    ):
        sp.files.append(FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath=root)]))

    # Extra file read allowances from platform extras
    for extra_path in policy.platform.seatbelt.extra_allow.file_read_extra:
        sp.files.append(FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath=_abs(extra_path).as_posix())]))

    # FS read/write from policy.fs
    fs = policy.fs
    read_dirs: list[Path] = []
    write_dirs: list[Path] = []
    read_seen: set[str] = set()
    write_seen: set[str] = set()

    # Write dirs
    for p in fs.write_paths:
        ap = _abs(p)
        d = ap if ap.is_dir() else ap.parent
        dp = d.as_posix()
        if dp not in write_seen:
            write_seen.add(dp)
            write_dirs.append(d)

    # Read dirs: include symlink path and resolved real path parents
    for p in fs.read_paths:
        ap = _abs(p)
        cand: list[Path] = []
        d = ap if ap.is_dir() else ap.parent
        cand.append(d)
        rp = ap.resolve(strict=False)
        rd = rp if rp.is_dir() else rp.parent
        cand.append(rd)
        for dd in cand:
            dp = dd.as_posix()
            if dp not in read_seen:
                read_seen.add(dp)
                read_dirs.append(dd)

    if write_dirs:
        sp.files.append(
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath=p.as_posix()) for p in write_dirs])
        )
    if read_dirs:
        sp.files.append(FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath=p.as_posix()) for p in read_dirs]))

    # Parent directory metadata to enable traversal
    meta_parents: set[Path] = set()

    def _add_parents(p: Path) -> None:
        cur = p
        while True:
            meta_parents.add(cur)
            if cur.as_posix() == "/":
                break
            cur = cur.parent

    for ap in read_dirs:
        _add_parents(ap)
    for ap in write_dirs:
        _add_parents(ap)
    for root in ("/opt", "/usr", "/private", "/System", "/Users"):
        meta_parents.add(Path(root))
    if meta_parents:
        sp.files.append(
            FileRule(
                op=FileOp.FILE_READ_METADATA,
                filters=[LiteralFilter(literal=p.as_posix()) for p in sorted(meta_parents, key=lambda q: q.as_posix())],
            )
        )

    # Network rules
    mode = policy.net.mode
    if mode == "open":
        sp.network.extend(
            [
                NetworkRule(op=NetworkOp.NETWORK_INBOUND),
                NetworkRule(op=NetworkOp.NETWORK_OUTBOUND),
                NetworkRule(op=NetworkOp.NETWORK_BIND),
            ]
        )
    elif mode == "loopback":
        sp.network.extend(
            [
                NetworkRule(op=NetworkOp.NETWORK_INBOUND, local_only=True),
                NetworkRule(op=NetworkOp.NETWORK_OUTBOUND, local_only=True),
                NetworkRule(op=NetworkOp.NETWORK_BIND, local_only=True),
            ]
        )
    # mode == "none": no network rules

    # Compile to SBPL text
    sb_text: str = compile_sbpl(sp)
    return sb_text


# -----------------------------
# CLI entry
# -----------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="sandboxer", description="Run a command under a YAML-defined sandbox (macOS seatbelt MVP)"
    )
    ap.add_argument("--policy", required=True, help="Path to policy.yaml (explicit-only schema)")
    ap.add_argument("--trace", action="store_true", help="Enable seatbelt trace logging")
    ap.add_argument("--debug", action="store_true", help="Verbose diagnostics (policy path, -D params)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (prefix with -- to separate)")
    args = ap.parse_args()

    if not args.cmd:
        print("sandboxer: missing command after --", file=sys.stderr)
        return 2
    # Drop a leading "--" separator if present
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("sandboxer: empty command", file=sys.stderr)
        return 2

    raw = yaml.safe_load(Path(args.policy).read_text())
    try:
        policy = Policy(**raw)
    except Exception as e:
        print(f"sandboxer: invalid policy YAML: {e}", file=sys.stderr)
        return 2

    # Allow debug via env flag as well (SJ_DEBUG_DIAG=1)
    if not args.debug and os.environ.get("SJ_DEBUG_DIAG"):
        args.debug = True
    if args.trace or policy.platform.trace:
        # Platform-neutral trace flag; individual backends may not support it
        policy.platform.trace = True

    # Platform gate: only macOS seatbelt for MVP
    if sys.platform != "darwin":
        print("sandboxer: unsupported platform for MVP (only macOS supported)", file=sys.stderr)
        return 3

    # Env construction for child
    child_env: dict[str, str] = {}
    for name in policy.env.passthrough:
        if name in os.environ:
            child_env[name] = os.environ[name]
    # Explicit set wins
    for k, v in (policy.env.set or {}).items():
        child_env[k] = str(v)

    # Ensure expected runtime dirs exist (TMPDIR/TMP/TEMP, MPLCONFIGDIR, PYTHONPYCACHEPREFIX)
    for key in ("TMPDIR", "TMP", "TEMP", "MPLCONFIGDIR", "PYTHONPYCACHEPREFIX"):
        if p := child_env.get(key):
            Path(p).mkdir(parents=True, exist_ok=True)

    # Compose SBPL policy file
    tmpdir = tempfile.mkdtemp(prefix="sandboxer-")
    # Write trace under a writable runtime dir (prefer TMPDIR from policy.env.set, then HOME), else tmpdir
    trace_path = None
    if policy.platform.trace or policy.platform.seatbelt.trace:
        env_set = policy.env.set or {}
        tmp_hint = env_set.get("TMPDIR") or env_set.get("TMP") or env_set.get("TEMP")
        home_dir = env_set.get("HOME") or os.environ.get("HOME")
        if tmp_hint:
            base = Path(tmp_hint)
        elif home_dir:
            base = Path(home_dir)
        else:
            base = Path(tmpdir)
        base.mkdir(parents=True, exist_ok=True)
        tp = base / "seatbelt.trace.log"
        tp.touch(exist_ok=True)
        trace_path = str(tp)
    sb_path = Path(tmpdir) / "policy.sb"
    sb_text = _compose_sbpl(policy, trace_path)
    sb_path.write_text(sb_text)
    if args.debug:
        print(f"sandboxer: policy at {sb_path}", file=sys.stderr)
    if trace_path:
        print(f"sandboxer: trace to {trace_path}", file=sys.stderr)
    # Optional policy echo for observability
    echo_dir = os.environ.get("SJ_POLICY_ECHO_DIR")
    if echo_dir:
        try:
            ed = Path(echo_dir)
            ed.mkdir(parents=True, exist_ok=True)
            (ed / "policy.sb").write_text(sb_text)
            if args.debug:
                print(f"sandboxer: echoed policy to {ed}", file=sys.stderr)
        except Exception as e:
            print(f"sandboxer: policy echo failed: {e}", file=sys.stderr)

    # Resolve sandbox-exec
    if not (sx := shutil.which("sandbox-exec")):
        print("sandboxer: sandbox-exec not found (macOS only)", file=sys.stderr)
        return 4

    # Execute under sandbox
    sx_args = [sx, "-f", sb_path, *cmd]
    if args.debug:
        print("sandboxer: exec:", " ".join(shlex.quote(str(x)) for x in sx_args), file=sys.stderr)
    proc = subprocess.Popen(sx_args, env=child_env)
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
