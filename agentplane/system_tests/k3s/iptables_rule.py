"""Add an iptables rule when it is absent, while sharing xtables with k3s and kube-proxy."""

import subprocess
from pathlib import Path

_LOCK_WAIT_SECONDS = "5"


class IptablesCommandError(RuntimeError):
    def __init__(self, command: tuple[str, ...], result: subprocess.CompletedProcess[str]) -> None:
        self.command = command
        self.returncode = result.returncode
        super().__init__(
            f"{command} exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )


def ensure_rule(iptables: Path, *, table: str, chain: str, rule: tuple[str, ...]) -> None:
    prefix = (str(iptables), "-w", _LOCK_WAIT_SECONDS, "-t", table)
    check = (*prefix, "-C", chain, *rule)
    result = subprocess.run(check, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    if result.returncode != 1:
        raise IptablesCommandError(check, result)

    append = (*prefix, "-A", chain, *rule)
    result = subprocess.run(append, capture_output=True, text=True, check=False)
    if result.returncode:
        raise IptablesCommandError(append, result)
