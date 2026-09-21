#!/usr/bin/env python3
"""Drive the QEMU guest's serial console non-interactively.

`relay` connects to the QEMU serial unix socket and forwards anything written
to cmd.fifo back to the guest; `run` and `push` are the client half. Booting
Android under TCG takes tens of minutes and the guest's mksh echoes commands
back with line-editor redraw noise, so every wait is against QEMU's own
chardev logfile rather than a live terminal.
"""

import base64
import os
import re
import select
import socket
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOCK = BASE / "serial.sock"
LOG = BASE / "console.log"
FIFO = BASE / "cmd.fifo"


def relay() -> None:
    for _ in range(600):
        if SOCK.exists():
            break
        time.sleep(0.5)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(SOCK))
    sock.setblocking(False)

    if not FIFO.exists():
        os.mkfifo(FIFO)
    # O_RDWR keeps the fifo open across writers, so select() never spins on EOF.
    fifo = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)

    # console.log is written by QEMU's own chardev logfile= (which captures
    # from power-on, before this relay can connect), so the relay only drains
    # the socket to keep the guest from blocking on a full buffer.
    while True:
        readers: list[socket.socket | int] = [sock, fifo]
        ready, _, _ = select.select(readers, [], [], 30.0)
        if sock in ready and not sock.recv(65536):
            return
        if fifo in ready:
            cmd = os.read(fifo, 65536)
            if cmd:
                sock.sendall(cmd)


def send(line: str) -> None:
    with FIFO.open("wb", buffering=0) as f:
        f.write(line.encode() + b"\n")


def run(cmd: str, timeout: float) -> None:
    """Send a shell line to the guest and print just that command's output.

    Completion is matched on the *expanded* marker (MARKERrc=<digits>), which
    only the executed echo can produce -- the echoed source still reads `$?`,
    so the redraw noise cannot be mistaken for the command finishing.
    """
    marker = f"__DONE{int(time.time() * 1000) % 10**9}__"
    start = LOG.stat().st_size
    send(f"{cmd}; echo {marker}rc=$?")
    done = re.compile(re.escape(marker) + r"rc=(\d+)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        with LOG.open("rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
        m = done.search(text)
        if m:
            body = text[: m.start()]
            # Drop the echoed command line so only real output is printed.
            print(body[body.find("\n") + 1 :].rstrip())
            print(f"[rc={m.group(1)}]")
            return
        time.sleep(1.0)
    raise SystemExit(f"TIMEOUT after {timeout}s running {cmd!r}")


def push(local: str, remote: str) -> None:
    """Copy a local file into the guest as chunked base64 appends.

    The tty line discipline caps a single input line at ~4 KiB, so a whole
    file cannot be sent as one echo.
    """
    blob = base64.b64encode(Path(local).read_bytes()).decode()
    run(f"rm -f {remote}.b64 {remote}", 60)
    for i in range(0, len(blob), 2000):
        send(f"echo -n '{blob[i : i + 2000]}' >> {remote}.b64")
        time.sleep(0.4)
    run(f"base64 -d < {remote}.b64 > {remote} && chmod 755 {remote} && ls -l {remote}", 120)


def main() -> None:
    mode = sys.argv[1]
    if mode == "relay":
        relay()
    elif mode == "run":
        run(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 120.0)
    elif mode == "push":
        push(sys.argv[2], sys.argv[3])
    elif mode == "send":
        send(" ".join(sys.argv[2:]))
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
