"""Observable RAM, disk and network workload for the standalone microVM experiment."""

import fcntl
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import time
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit


def command(argv: list[str], *, user: int | None = None) -> dict[str, object]:
    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=12,
        user=user,
        group=user,
        extra_groups=[] if user is not None else None,
    )
    return {"code": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]}


class Workload:
    def __init__(self, stack: ExitStack, owner: TextIO, open_file: TextIO) -> None:
        self.nonce = secrets.token_hex(32)
        for retained in (Path("/state/retained"), Path("/state/workspaces/retained")):
            if not retained.exists():
                retained.write_text(self.nonce)
        self.memory = secrets.token_bytes(8 * 1024 * 1024)
        self.counter = 0
        self.owner = owner
        fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.open_file = open_file
        self.open_file.write("hello world\n")
        self.open_file.flush()
        os.fsync(self.open_file.fileno())
        self.open_file.seek(6)
        self.child = stack.enter_context(subprocess.Popen(["/bin/sleep", "100000"]))
        stack.callback(self.child.terminate)
        self.db = stack.enter_context(closing(sqlite3.connect("/state/runtime-resources.db")))
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=EXTRA")
        self.db.execute("CREATE TABLE IF NOT EXISTS proof (value INTEGER)")
        self.db.execute("INSERT INTO proof VALUES (1)")
        self.db.commit()

    def state(self) -> dict[str, object]:
        return {
            "nonce": self.nonce,
            "memory_sha256": hashlib.sha256(self.memory).hexdigest(),
            "counter": self.counter,
            "pid": os.getpid(),
            "child_pid": self.child.pid,
            "child_alive": self.child.poll() is None,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "file_offset": self.open_file.tell(),
            "file_inode": os.fstat(self.open_file.fileno()).st_ino,
            "sqlite_integrity": self.db.execute("PRAGMA integrity_check").fetchone()[0],
            "sqlite_rows": self.db.execute("SELECT count(*) FROM proof").fetchone()[0],
            "retained_state": Path("/state/retained").read_text(),
            "retained_workspace": Path("/state/workspaces/retained").read_text(),
            "wall_time": time.time(),
            "monotonic_time": time.monotonic(),
        }

    def proof(self) -> dict[str, object]:
        private = "/run/agentplane/environment-runtime-tls/tls.key"
        return {
            "mounts": Path("/proc/mounts").read_text(),
            "devices": command(["lsblk", "-o", "NAME,SERIAL,FSTYPE,MOUNTPOINTS", "--json"]),
            "runner_help": command(["/run/agentplane/environment/bin/agentplane-environment-runtime", "--help"]),
            "execution_namespace_guard": command(
                ["/run/agentplane/environment/bin/agentplane-execution-service", "companion"]
            ),
            "runner_disk_write": command(["touch", "/run/agentplane/environment/should-fail"]),
            "config_disk_write": command(["touch", "/run/agentplane/config/should-fail"]),
            "credential_as_root": Path(private).read_text().strip(),
            "credential_as_workload": command(["cat", private], user=107),
            "state_owner_contender": command(
                [
                    "python3",
                    "-c",
                    "import fcntl; f=open('/state/spike-owner.lock','a+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                ]
            ),
            "config": json.loads(Path("/run/agentplane/config/environment-runtime.json").read_text()),
            "launch": json.loads(Path("/run/agentplane/sandbox/launch.json").read_text()),
            "trust_readable_as_workload": command(
                ["test", "-r", "/run/agentplane/trust/ca-certificates.crt"], user=107
            ),
        }


class Handler(BaseHTTPRequestHandler):
    workload: Workload

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        result: dict[str, object]
        if parsed.path == "/state":
            result = self.workload.state()
        elif parsed.path == "/advance":
            self.workload.counter += 1
            result = self.workload.state()
        elif parsed.path == "/proof":
            result = self.workload.proof()
        elif parsed.path == "/sync":
            os.sync()
            result = {"synced": True}
        elif parsed.path == "/read-fd":
            result = {"read": self.workload.open_file.read(), "offset": self.workload.open_file.tell()}
        elif parsed.path == "/network":
            argv = ["curl", "--silent", "--show-error", "--fail", "--max-time", "3", "--noproxy", ""]
            if params.get("proxy"):
                argv += ["--proxy", params["proxy"][0], "--proxy-header", "Proxy-Authorization: Bearer forged-guest"]
                argv += ["--proxy-header", "X-Agentplane-Workload-Token: forged-audience forged-guest"]
            else:
                argv += ["--proxy", ""]
            if params.get("resolve"):
                argv += ["--resolve", params["resolve"][0]]
            result = command([*argv, params["url"][0]])
        elif parsed.path == "/tcp":
            try:
                with socket.create_connection((params["host"][0], int(params["port"][0])), timeout=2):
                    result = {"connected": True}
            except OSError as exc:
                result = {"connected": False, "error": str(exc)}
        elif parsed.path == "/udp":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                    connection.settimeout(2)
                    connection.connect((params["host"][0], int(params["port"][0])))
                    connection.send(b"spike-udp")
                    result = {"echo": connection.recv(64).decode()}
            except OSError as exc:
                result = {"error": str(exc)}
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    with (
        ExitStack() as stack,
        Path("/state/spike-owner.lock").open("a+") as owner,
        Path("/state/workspaces/open-file").open("w+") as open_file,
    ):
        Handler.workload = Workload(stack, owner, open_file)
        print("HELLO WORLD: Firecracker guest workload ready", flush=True)
        server = stack.enter_context(HTTPServer(("0.0.0.0", 8080), Handler))
        server.serve_forever()


if __name__ == "__main__":
    main()
