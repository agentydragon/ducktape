"""Structured probe for BuildBuddy `bb remote` runner reuse.

This script runs inside the BuildBuddy runner VM. It records explicit,
non-secret data into a persistent probe directory so a later recycled VM can
read the previous run's local probe logs.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "ducktape.bb_runner_probe.v1"
DEFAULT_DIR = Path(os.environ.get("CI_VM_PROBE_DIR", "/home/buildbuddy/workspace/.ducktape-ci-vm-probe"))
SAFE_ENV_KEYS = ("BUILD_WORKSPACE_DIRECTORY", "GIT_REPO_DEFAULT_BRANCH", "HOME", "PWD", "RBE_IMAGE", "USER")
PATHS_TO_STAT = (
    "/home/buildbuddy/workspace",
    "/home/buildbuddy/workspace/repo-root",
    "/home/buildbuddy/workspace/output-base",
    "/home/buildbuddy/workspace/output-base/server",
)
PROC_GLOBAL_FILES = (
    (Path("/proc/sys/kernel/random/boot_id"), "global/boot_id"),
    (Path("/proc/uptime"), "global/uptime"),
    (Path("/proc/stat"), "global/stat"),
    (Path("/proc/meminfo"), "global/meminfo"),
    (Path("/proc/self/cgroup"), "global/self_cgroup"),
    (Path("/proc/self/mountinfo"), "global/self_mountinfo"),
)
PROC_PROCESS_FILES = ("cmdline", "stat", "status", "statm", "io", "cgroup", "limits", "schedstat")
DIGEST_RE = re.compile(r"(?P<digest>(?:/compressed-blobs/[^\s]+|/blobs/[^\s]+|[a-f0-9]{64}/\d+))", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class ProbePaths:
    root: Path
    current: Path
    latest: Path
    probes_jsonl: Path
    archive: Path
    latest_jsonl: Path


@dataclasses.dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


def probe_paths(root: Path) -> ProbePaths:
    current = root / "current"
    latest = root / "latest"
    return ProbePaths(
        root=root,
        current=current,
        latest=latest,
        probes_jsonl=current / "probes.jsonl",
        archive=current / "probe.tgz",
        latest_jsonl=latest / "probes.jsonl",
    )


def iso_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def read_text(path: Path, *, max_bytes: int = 1_000_000) -> str | None:
    try:
        with path.open("rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace").strip()
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.split()[0])
    except (IndexError, ValueError):
        return None


def run_command(argv: list[str], *, timeout: float = 2.0) -> CommandResult:
    try:
        proc = subprocess.run(argv, check=False, text=True, capture_output=True, timeout=timeout)
    except Exception as e:
        return CommandResult(argv=argv, returncode=None, stdout="", stderr="", error=repr(e))
    return CommandResult(argv=argv, returncode=proc.returncode, stdout=proc.stdout.strip(), stderr=proc.stderr.strip())


def command_json(result: CommandResult) -> dict[str, Any]:
    return dataclasses.asdict(result)


def safe_env() -> dict[str, str]:
    return {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}


def path_stat(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError as e:
        return {"path": str(path), "exists": False, "error": repr(e)}
    return {
        "path": str(path),
        "exists": True,
        "mode": oct(st.st_mode),
        "inode": st.st_ino,
        "dev": st.st_dev,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "ctime": st.st_ctime,
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
    }


def safe_path_component(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in value)


def parse_proc_stat(text: str) -> dict[str, Any]:
    # Raw procfs files are archived separately. This parser is only for the
    # compact log summary fields that make CI output scannable.
    # /proc/<pid>/stat is: pid (comm with spaces) state ppid ... starttime ...
    close = text.rfind(")")
    if close == -1:
        return {}
    prefix = text[: close + 1]
    rest = text[close + 2 :].split()
    if len(rest) < 20:
        return {}
    return {
        "pid": int(prefix.split(" ", 1)[0]),
        "comm": prefix.split(" ", 1)[1][1:-1],
        "state": rest[0],
        "ppid": int(rest[1]),
        "start_ticks": int(rest[19]),
    }


def proc_btime() -> int | None:
    text = read_text(Path("/proc/stat"))
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def is_bazel_server_argv(argv: list[str]) -> bool:
    joined = "\0".join(argv)
    return "A-server.jar" in joined or any(arg.startswith("bazel(") for arg in argv)


def process_info(pid_dir: Path, *, boot_time: int | None, ticks_per_second: int) -> dict[str, Any] | None:
    if not pid_dir.name.isdigit():
        return None
    cmdline_raw = read_text(pid_dir / "cmdline", max_bytes=256_000)
    if not cmdline_raw:
        return None
    argv = [part for part in cmdline_raw.split("\x00") if part]
    if not is_bazel_server_argv(argv):
        return None

    stat = parse_proc_stat(read_text(pid_dir / "stat") or "")
    start_time = None
    age_seconds = None
    if boot_time is not None and stat.get("start_ticks") is not None:
        start_epoch = boot_time + (stat["start_ticks"] / ticks_per_second)
        start_time = dt.datetime.fromtimestamp(start_epoch, dt.UTC).isoformat()
        age_seconds = max(0.0, time.time() - start_epoch)

    try:
        cwd = str((pid_dir / "cwd").readlink())
    except OSError:
        cwd = None

    return {
        "pid": int(pid_dir.name),
        "argv": argv,
        "cmdline_sha256": hashlib.sha256(cmdline_raw.encode()).hexdigest(),
        "stat": stat,
        "start_time": start_time,
        "age_seconds": age_seconds,
        "cwd": cwd,
    }


def bazel_servers() -> list[dict[str, Any]]:
    boot_time = proc_btime()
    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    result = []
    for pid_dir in Path("/proc").iterdir():
        try:
            info = process_info(pid_dir, boot_time=boot_time, ticks_per_second=ticks)
        except OSError:
            continue
        if info is not None:
            result.append(info)
    return sorted(result, key=lambda p: p["pid"])


def capture_bytes(src: Path, dest: Path, *, max_bytes: int = 2_000_000) -> dict[str, Any]:
    rel = str(dest)
    try:
        with src.open("rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        return {"source": str(src), "path": rel, "exists": False, "error": repr(e)}
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {
        "source": str(src),
        "path": rel,
        "exists": True,
        "size": len(data),
        "sha256": sha256_bytes(data),
        "truncated": truncated,
    }


def capture_symlink(src: Path, dest: Path) -> dict[str, Any]:
    try:
        target = str(src.readlink())
    except OSError as e:
        return {"source": str(src), "path": str(dest), "exists": False, "error": repr(e)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(target + "\n")
    return {
        "source": str(src),
        "path": str(dest),
        "exists": True,
        "target": target,
        "size": dest.stat().st_size,
        "sha256": sha256_file(dest),
    }


def capture_proc_snapshot(paths: ProbePaths, phase: str, servers: list[dict[str, Any]]) -> dict[str, Any]:
    root = paths.current / "proc" / safe_path_component(phase)
    files = []
    for src, rel in PROC_GLOBAL_FILES:
        files.append(capture_bytes(src, root / rel))
    processes = []
    for server in servers:
        pid = str(server["pid"])
        proc_dir = Path("/proc") / pid
        dest_dir = root / "bazel_servers" / pid
        proc_files = [capture_bytes(proc_dir / name, dest_dir / name) for name in PROC_PROCESS_FILES]
        proc_files.append(capture_symlink(proc_dir / "cwd", dest_dir / "cwd.txt"))
        proc_files.append(capture_symlink(proc_dir / "exe", dest_dir / "exe.txt"))
        processes.append({"pid": server["pid"], "files": proc_files})
    return {"root": str(root), "global_files": files, "processes": processes}


def summarize_jsonl(path: Path, *, tail: int = 5, max_bytes: int = 5_000_000) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    data = path.read_bytes()
    truncated = False
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        truncated = True
    entries = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"raw": line})
    phases = [entry.get("phase") for entry in entries if isinstance(entry, dict)]
    last = entries[-1] if entries else None
    tail_summaries = []
    for entry in entries[-tail:]:
        if not isinstance(entry, dict):
            tail_summaries.append({"raw": entry.get("raw") if isinstance(entry, dict) else str(entry)})
            continue
        tail_summaries.append(
            {
                "phase": entry.get("phase"),
                "timestamp": entry.get("timestamp"),
                "boot_id": entry.get("boot_id"),
                "uptime_seconds": entry.get("uptime_seconds"),
                "bazel_servers": [
                    {
                        "pid": p.get("pid"),
                        "start_time": p.get("start_time"),
                        "age_seconds": p.get("age_seconds"),
                        "cmdline_sha256": p.get("cmdline_sha256"),
                    }
                    for p in entry.get("bazel_servers", [])
                ],
                "git_head": entry.get("git", {}).get("head", {}).get("stdout"),
            }
        )
    return {
        "exists": True,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "truncated": truncated,
        "entry_count_in_sample": len(entries),
        "phases_in_sample": phases,
        "first_timestamp_in_sample": entries[0].get("timestamp") if entries else None,
        "last_timestamp_in_sample": last.get("timestamp") if isinstance(last, dict) else None,
        "last_phase": last.get("phase") if isinstance(last, dict) else None,
        "last_bazel_servers": [
            {
                "pid": p.get("pid"),
                "start_time": p.get("start_time"),
                "age_seconds": p.get("age_seconds"),
                "cmdline_sha256": p.get("cmdline_sha256"),
            }
            for p in (last or {}).get("bazel_servers", [])
        ]
        if isinstance(last, dict)
        else [],
        "tail_summaries": tail_summaries,
    }


def prepare_current(paths: ProbePaths, phase: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    previous_run_log = summarize_jsonl(paths.latest_jsonl)
    stale_current = None
    if phase == "before-test":
        current = summarize_jsonl(paths.probes_jsonl)
        if current.get("exists") and current.get("sha256") != previous_run_log.get("sha256"):
            stale_current = current
        shutil.rmtree(paths.current, ignore_errors=True)
    paths.current.mkdir(parents=True, exist_ok=True)
    return previous_run_log, stale_current


def snapshot(
    phase: str, paths: ProbePaths, previous_run_log: dict[str, Any], stale_current: dict[str, Any] | None
) -> dict[str, Any]:
    uname = os.uname()
    git = {
        "head": run_command(["git", "rev-parse", "HEAD"]),
        "branch": run_command(["git", "branch", "--show-current"]),
        "status": run_command(["git", "status", "--short", "--branch"], timeout=5.0),
        "last_commit": run_command(["git", "log", "-1", "--format=%H%x00%P%x00%ct%x00%s"]),
    }
    servers = bazel_servers()
    proc_snapshot = capture_proc_snapshot(paths, phase, servers)
    return {
        "schema": SCHEMA,
        "phase": phase,
        "timestamp": iso_now(),
        "hostname": socket.gethostname(),
        "uname": {
            "sysname": uname.sysname,
            "nodename": uname.nodename,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
        },
        "cwd": str(Path.cwd()),
        "probe_dir": str(paths.root),
        "env": safe_env(),
        "boot_id": read_text(Path("/proc/sys/kernel/random/boot_id")),
        "uptime_seconds": parse_float(read_text(Path("/proc/uptime"))),
        "previous_run_log": previous_run_log,
        "stale_current_log": stale_current,
        "paths": [path_stat(Path(p)) for p in PATHS_TO_STAT]
        + [
            path_stat(paths.root),
            path_stat(paths.current),
            path_stat(paths.latest),
            path_stat(paths.probes_jsonl),
            path_stat(paths.latest_jsonl),
        ],
        "git": git,
        "bazel_servers": servers,
        "proc_snapshot": proc_snapshot,
    }


def snapshot_for_json(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["git"] = {k: command_json(v) for k, v in data["git"].items()}
    return out


def append_snapshot(out: Path, data: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot_for_json(data), sort_keys=True) + "\n")


def print_summary(data: dict[str, Any], out: Path) -> None:
    previous = data["previous_run_log"].get("exists", False)
    stale = bool((data.get("stale_current_log") or {}).get("exists"))
    print(
        "CI_VM_PROBE_SUMMARY "
        f"phase={data['phase']} "
        f"out={out} "
        f"previous_run_log={'yes' if previous else 'no'} "
        f"stale_current_log={'yes' if stale else 'no'} "
        f"bazel_servers={len(data['bazel_servers'])} "
        f"boot_id={data.get('boot_id')} "
        f"uptime={data.get('uptime_seconds')}"
    )
    for proc in data["bazel_servers"]:
        print(
            "CI_VM_PROBE_SERVER "
            f"pid={proc['pid']} age={proc.get('age_seconds', 0):.1f}s "
            f"start={proc.get('start_time')} sha256={proc['cmdline_sha256']}"
        )


def cmd_snapshot(args: argparse.Namespace) -> int:
    paths = probe_paths(Path(args.dir))
    previous_run_log, stale_current = prepare_current(paths, args.phase)
    data = snapshot(args.phase, paths, previous_run_log, stale_current)
    append_snapshot(paths.probes_jsonl, data)
    print_summary(data, paths.probes_jsonl)
    return 0


def make_archive(paths: ProbePaths) -> dict[str, Any]:
    paths.current.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": SCHEMA,
        "timestamp": iso_now(),
        "probe_dir": str(paths.root),
        "probes_jsonl": str(paths.probes_jsonl),
        "probes_jsonl_exists": paths.probes_jsonl.exists(),
    }
    manifest_path = paths.current / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with tarfile.open(paths.archive, "w:gz") as tar:
        if paths.probes_jsonl.exists():
            tar.add(paths.probes_jsonl, arcname="probes.jsonl")
        proc_dir = paths.current / "proc"
        if proc_dir.exists():
            tar.add(proc_dir, arcname="proc")
        if paths.latest_jsonl.exists():
            tar.add(paths.latest_jsonl, arcname="previous/probes.jsonl")
        previous_proc_dir = paths.latest / "proc"
        if previous_proc_dir.exists():
            tar.add(previous_proc_dir, arcname="previous/proc")
        tar.add(manifest_path, arcname="manifest.json")
    return {"path": str(paths.archive), "size": paths.archive.stat().st_size, "sha256": sha256_file(paths.archive)}


def persist_latest(paths: ProbePaths) -> None:
    tmp = paths.root / "latest.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(paths.current, tmp)
    shutil.rmtree(paths.latest, ignore_errors=True)
    tmp.rename(paths.latest)


def upload_archive(archive: Path) -> dict[str, Any]:
    bb = shutil.which("bb")
    if bb is None:
        return {"attempted": False, "reason": "bb not found on PATH"}
    result = run_command([bb, "upload", str(archive)], timeout=30.0)
    return {
        "attempted": True,
        "digest": extract_digest(result.stdout) or extract_digest(result.stderr),
        "result": command_json(result),
    }


def extract_digest(text: str) -> str:
    match = DIGEST_RE.search(text)
    return match.group("digest") if match else ""


def output_tail(text: str, *, max_bytes: int = 500) -> str:
    return text[-max_bytes:]


def cmd_finalize(args: argparse.Namespace) -> int:
    paths = probe_paths(Path(args.dir))
    archive_info = make_archive(paths)
    persist_latest(paths)
    print(
        f"CI_VM_PROBE_ARCHIVE path={archive_info['path']} size={archive_info['size']} sha256={archive_info['sha256']}"
    )
    if args.upload:
        upload = upload_archive(paths.archive)
        if upload.get("attempted") and upload["result"]["returncode"] == 0 and upload.get("digest"):
            digest = upload["digest"]
            print(f"CI_VM_PROBE_CAS digest={digest}")
        elif upload.get("attempted") and upload["result"]["returncode"] == 0:
            result = upload["result"]
            print(
                "CI_VM_PROBE_CAS "
                "missing_digest="
                + json.dumps(
                    {
                        "stdout_len": len(result["stdout"]),
                        "stderr_len": len(result["stderr"]),
                        "stdout_tail": output_tail(result["stdout"]),
                        "stderr_tail": output_tail(result["stderr"]),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"CI_VM_PROBE_CAS unavailable={json.dumps(upload, sort_keys=True)}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("phase")
    snap.add_argument("--dir", default=str(DEFAULT_DIR))
    snap.set_defaults(func=cmd_snapshot)

    final = sub.add_parser("finalize")
    final.add_argument("--dir", default=str(DEFAULT_DIR))
    final.add_argument("--upload", action="store_true")
    final.set_defaults(func=cmd_finalize)
    return p


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
