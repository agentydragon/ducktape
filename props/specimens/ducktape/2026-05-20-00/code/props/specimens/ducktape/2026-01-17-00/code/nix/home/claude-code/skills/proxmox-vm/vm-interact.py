#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "typer", "platformdirs"]
# ///
"""Interact with Proxmox VMs via QEMU monitor.

Run actions sequentially on a VM. Actions are executed in order.

Examples:
    ./vm-interact.py 110 --screenshot
    ./vm-interact.py 110 --type "ip addr" --enter --screenshot
    ./vm-interact.py 110 --sendkey ctrl-c --screenshot
    ./vm-interact.py 110 --info
    echo -e "type ip addr\\nenter\\nscreenshot" | ./vm-interact.py 110 --stdin
"""

import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import platformdirs
import typer
from PIL import Image

PROXMOX_HOST = "root@atlas"
PROXMOX_NODE = "atlas"

# Map characters to QEMU sendkey names
CHAR_TO_KEY = {
    "a": "a",
    "b": "b",
    "c": "c",
    "d": "d",
    "e": "e",
    "f": "f",
    "g": "g",
    "h": "h",
    "i": "i",
    "j": "j",
    "k": "k",
    "l": "l",
    "m": "m",
    "n": "n",
    "o": "o",
    "p": "p",
    "q": "q",
    "r": "r",
    "s": "s",
    "t": "t",
    "u": "u",
    "v": "v",
    "w": "w",
    "x": "x",
    "y": "y",
    "z": "z",
    "A": "shift-a",
    "B": "shift-b",
    "C": "shift-c",
    "D": "shift-d",
    "E": "shift-e",
    "F": "shift-f",
    "G": "shift-g",
    "H": "shift-h",
    "I": "shift-i",
    "J": "shift-j",
    "K": "shift-k",
    "L": "shift-l",
    "M": "shift-m",
    "N": "shift-n",
    "O": "shift-o",
    "P": "shift-p",
    "Q": "shift-q",
    "R": "shift-r",
    "S": "shift-s",
    "T": "shift-t",
    "U": "shift-u",
    "V": "shift-v",
    "W": "shift-w",
    "X": "shift-x",
    "Y": "shift-y",
    "Z": "shift-z",
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    " ": "spc",
    "\n": "ret",
    "\t": "tab",
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave_accent",
    ",": "comma",
    ".": "dot",
    "/": "slash",
    "!": "shift-1",
    "@": "shift-2",
    "#": "shift-3",
    "$": "shift-4",
    "%": "shift-5",
    "^": "shift-6",
    "&": "shift-7",
    "*": "shift-8",
    "(": "shift-9",
    ")": "shift-0",
    "_": "shift-minus",
    "+": "shift-equal",
    "{": "shift-bracket_left",
    "}": "shift-bracket_right",
    "|": "shift-backslash",
    ":": "shift-semicolon",
    '"': "shift-apostrophe",
    "~": "shift-grave_accent",
    "<": "shift-comma",
    ">": "shift-dot",
    "?": "shift-slash",
}


def get_cache_dir(vmid: int) -> Path:
    cache_dir = Path(platformdirs.user_cache_dir("proxmox-vm")) / f"vm{vmid}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def ssh_cmd(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", PROXMOX_HOST, cmd], check=True, capture_output=True, text=True)


def qemu_monitor(vmid: int, command: str) -> str:
    result = ssh_cmd(f"echo '{command}' | qm monitor {vmid}")
    return result.stdout


def pvesh(method: str, path: str) -> str:
    result = ssh_cmd(f"pvesh {method} {path}")
    return result.stdout


def do_sendkey(vmid: int, key: str) -> None:
    pvesh("create", f"/nodes/{PROXMOX_NODE}/qemu/{vmid}/monitor -command 'sendkey {key}'")


def do_screenshot(vmid: int, delay: float) -> Path:
    remote_ppm = f"/tmp/vm{vmid}-screenshot.ppm"
    cache_dir = get_cache_dir(vmid)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_png = cache_dir / f"{timestamp}.png"

    qemu_monitor(vmid, f"screendump {remote_ppm}")

    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp:
        local_ppm = Path(tmp.name)

    subprocess.run(["scp", f"{PROXMOX_HOST}:{remote_ppm}", str(local_ppm)], check=True, capture_output=True)

    img = Image.open(local_ppm)
    img.save(local_png)

    local_ppm.unlink(missing_ok=True)
    ssh_cmd(f"rm -f {remote_ppm}")

    typer.echo(f"Screenshot: {local_png}")
    time.sleep(delay)
    return local_png


def do_type(vmid: int, text: str, delay: float) -> None:
    for char in text:
        key = CHAR_TO_KEY.get(char)
        if key is None:
            typer.echo(f"Warning: Unknown character '{char}', skipping", err=True)
            continue
        do_sendkey(vmid, key)
        time.sleep(delay)
    typer.echo(f"Typed: {text!r}")


def do_enter(vmid: int, delay: float) -> None:
    do_sendkey(vmid, "ret")
    typer.echo("Sent: Enter")
    time.sleep(delay)


def do_key(vmid: int, key: str, delay: float) -> None:
    do_sendkey(vmid, key)
    typer.echo(f"Sent: {key}")
    time.sleep(delay)


def do_info(vmid: int) -> None:
    result = ssh_cmd(f"qm guest cmd {vmid} network-get-interfaces")
    data = json.loads(result.stdout)

    typer.echo(f"VM {vmid} Network Interfaces:")
    for iface in data:
        name = iface.get("name", "unknown")
        mac = iface.get("hardware-address", "")
        ips = iface.get("ip-addresses", [])

        if name == "lo":
            continue

        typer.echo(f"  {name} ({mac}):")
        for ip in ips:
            addr = ip.get("ip-address", "")
            prefix = ip.get("prefix", "")
            iptype = ip.get("ip-address-type", "")
            typer.echo(f"    {addr}/{prefix} ({iptype})")


def do_sleep(seconds: float) -> None:
    typer.echo(f"Sleeping {seconds}s...")
    time.sleep(seconds)


def run_stdin_commands(vmid: int, delay: float) -> None:
    """Read commands from stdin, one per line."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "screenshot":
            do_screenshot(vmid, delay)
        elif cmd == "type":
            do_type(vmid, arg, delay)
        elif cmd == "enter":
            do_enter(vmid, delay)
        elif cmd == "sendkey":
            do_key(vmid, arg, delay)
        elif cmd == "info":
            do_info(vmid)
        elif cmd == "sleep":
            do_sleep(float(arg) if arg else 1.0)
        else:
            typer.echo(f"Unknown command: {cmd}", err=True)


def main(
    vmid: Annotated[int, typer.Argument(help="VM ID")],
    screenshot: Annotated[list[bool] | None, typer.Option("--screenshot", "-s", help="Take screenshot")] = None,
    type_text: Annotated[list[str] | None, typer.Option("--type", "-t", help="Type text")] = None,
    enter: Annotated[list[bool] | None, typer.Option("--enter", "-e", help="Press Enter")] = None,
    sendkey: Annotated[
        list[str] | None, typer.Option("--sendkey", "-k", help="Send QEMU key (e.g., ctrl-c, ret, shift-a)")
    ] = None,
    info: Annotated[bool, typer.Option("--info", "-i", help="Show VM network info")] = False,
    sleep: Annotated[list[float] | None, typer.Option("--sleep", help="Sleep for N seconds")] = None,
    stdin: Annotated[bool, typer.Option("--stdin", help="Read commands from stdin")] = False,
    delay: Annotated[float, typer.Option("--delay", "-d", help="Delay between keys in seconds")] = 0.05,
) -> None:
    """Interact with Proxmox VMs via QEMU monitor.

    Actions are executed in the order they appear on the command line.
    """
    if stdin:
        run_stdin_commands(vmid, delay)
        return

    # Build action list from sys.argv to preserve order
    actions: list[tuple[str, str | None]] = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--screenshot", "-s"):
            actions.append(("screenshot", None))
        elif arg in ("--enter", "-e"):
            actions.append(("enter", None))
        elif arg in ("--info", "-i"):
            actions.append(("info", None))
        elif arg.startswith(("--type=", "-t=")):
            actions.append(("type", arg.split("=", 1)[1]))
        elif arg in ("--type", "-t"):
            i += 1
            actions.append(("type", sys.argv[i]))
        elif arg.startswith(("--sendkey=", "-k=")):
            actions.append(("sendkey", arg.split("=", 1)[1]))
        elif arg in ("--sendkey", "-k"):
            i += 1
            actions.append(("sendkey", sys.argv[i]))
        elif arg.startswith("--sleep="):
            actions.append(("sleep", arg.split("=", 1)[1]))
        elif arg == "--sleep":
            i += 1
            actions.append(("sleep", sys.argv[i]))
        # Skip other args (vmid, --delay, --stdin, etc)
        i += 1

    if not actions and not info:
        typer.echo("No actions specified. Use --help for usage.", err=True)
        raise typer.Exit(1)

    if info and not actions:
        do_info(vmid)
        return

    for action, value in actions:
        if action == "screenshot":
            do_screenshot(vmid, delay)
        elif action == "type":
            do_type(vmid, value or "", delay)
        elif action == "enter":
            do_enter(vmid, delay)
        elif action == "sendkey":
            do_key(vmid, value or "", delay)
        elif action == "info":
            do_info(vmid)
        elif action == "sleep":
            do_sleep(float(value) if value else 1.0)


if __name__ == "__main__":
    typer.run(main)
