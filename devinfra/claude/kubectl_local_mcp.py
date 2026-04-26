"""Launch kubernetes-mcp-server with an in-memory (memfd) kubeconfig.

The kubeconfig is placed in an anonymous memfd — no filesystem path, no temp
file to clean up. The fd is inherited across execvp (created without
MFD_CLOEXEC) and disappears when the server process exits.

Why bearer token / Authentik JWT: see devinfra/k8s/kubeconfig.py's module docstring.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Repo root is two levels up from devinfra/claude/. Add it so the standard
# devinfra.* package tree is importable when running as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from devinfra.k8s.kubeconfig import build_kubeconfig, decrypt_jwt  # noqa: E402


def _sops_age_key() -> str | None:
    """Return SOPS_AGE_KEY from env, or derive from SSH key on CLI."""
    if key := os.environ.get("SOPS_AGE_KEY"):
        return key
    result = subprocess.run(
        ["ssh-to-age", "--private-key", "-i", Path("~/.ssh/id_ed25519").expanduser()],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() or None


def main() -> None:
    token = decrypt_jwt(_REPO_ROOT, sops_age_key=_sops_age_key())
    kubeconfig = build_kubeconfig(token)
    serialized = yaml.safe_dump(kubeconfig, default_flow_style=False, sort_keys=False).encode()

    # flags=0: no MFD_CLOEXEC, so the fd is inherited by the exec'd process.
    fd = os.memfd_create("kubeconfig", flags=0)
    os.write(fd, serialized)
    os.lseek(fd, 0, os.SEEK_SET)

    cmd = ["kubernetes-mcp-server", "--kubeconfig", f"/proc/self/fd/{fd}", "--disable-multi-cluster", *sys.argv[1:]]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
