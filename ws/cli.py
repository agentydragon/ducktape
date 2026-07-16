"""`ws` — disposable agent workspaces (cluster/k8s/agents/agent-sandbox/).

Thin kubectl wrapper around SandboxClaims: `new`/`ls`/`sh`/`extend`/`rm`.
Auth is whatever kubeconfig kubectl resolves (operator-only namespace).
"""

import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

NAMESPACE = "agent-workspaces"
WARM_POOL = "workspace"
CONTAINER = "workspace"
_ADOPTION_TIMEOUT_S = 120

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _kubectl(*args: str, capture: bool = True) -> str:
    result = subprocess.run(["kubectl", "-n", NAMESPACE, *args], capture_output=capture, text=True, check=True)
    return result.stdout if capture else ""


def parse_ttl(ttl: str) -> timedelta:
    """'90m' / '8h' / '3d' → timedelta."""
    if not (m := re.fullmatch(r"(\d+)([mhd])", ttl)):
        raise typer.BadParameter(f"{ttl=} — want e.g. 90m, 8h, 3d")
    count = int(m.group(1))
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: count})


def shutdown_time(ttl: str, now: datetime) -> str:
    return (now + parse_ttl(ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_manifest(name: str, ttl: str, now: datetime) -> dict:
    return {
        "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
        "kind": "SandboxClaim",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "warmPoolRef": {"name": WARM_POOL},
            "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": shutdown_time(ttl, now)},
        },
    }


def _claims() -> list[dict]:
    return json.loads(_kubectl("get", "sandboxclaims", "-o", "json"))["items"]


def _pods_by_name() -> dict[str, dict]:
    pods = json.loads(_kubectl("get", "pods", "-o", "json"))["items"]
    return {p["metadata"]["name"]: p for p in pods}


def _bound_sandbox(claim_name: str) -> str:
    deadline = time.monotonic() + _ADOPTION_TIMEOUT_S
    while time.monotonic() < deadline:
        claim = json.loads(_kubectl("get", "sandboxclaim", claim_name, "-o", "json"))
        if sandbox := claim.get("status", {}).get("sandbox", {}).get("name"):
            return sandbox
        time.sleep(2)
    typer.echo(f"claim {claim_name!r} not bound after {_ADOPTION_TIMEOUT_S}s", err=True)
    raise typer.Exit(1)


def _newest_claim_name() -> str:
    claims = sorted(_claims(), key=lambda c: c["metadata"]["creationTimestamp"])
    if not claims:
        typer.echo("no claims — `ws new` first", err=True)
        raise typer.Exit(1)
    return claims[-1]["metadata"]["name"]


def _exec_shell(sandbox: str) -> None:
    # Attach-or-create a persistent tmux session: dropped connections and
    # repeat `ws sh` land back in the same shell (tmux ships in the image).
    os.execvp(
        "kubectl",
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            "-it",
            sandbox,
            "-c",
            CONTAINER,
            "--",
            "tmux",
            "new-session",
            "-A",
            "-s",
            "main",
        ],
    )


@app.command()
def new(
    name: Annotated[str | None, typer.Argument(help="claim name (default: ws-<HHMMSS>)")] = None,
    ttl: Annotated[str, typer.Option(help="lifetime before auto-delete, e.g. 90m/8h/3d")] = "8h",
    shell: Annotated[bool, typer.Option(help="exec into the workspace once bound")] = True,
) -> None:
    """Claim a warm workspace (ready in seconds) and drop into it."""
    now = datetime.now(UTC)
    name = name or f"ws-{now:%H%M%S}"
    manifest = json.dumps(claim_manifest(name, ttl, now))
    subprocess.run(["kubectl", "-n", NAMESPACE, "apply", "-f", "-"], input=manifest, text=True, check=True)
    sandbox = _bound_sandbox(name)
    typer.echo(f"{name} → {sandbox} (dies {shutdown_time(ttl, now)})")
    if shell:
        _exec_shell(sandbox)


@app.command()
def ls() -> None:
    """List claims: name, sandbox, pod phase, deadline."""
    pods = _pods_by_name()
    rows = []
    for c in _claims():
        sandbox = c.get("status", {}).get("sandbox", {}).get("name", "-")
        phase = pods.get(sandbox, {}).get("status", {}).get("phase", "-")
        deadline = c["spec"].get("lifecycle", {}).get("shutdownTime", "-")
        rows.append((c["metadata"]["name"], sandbox, phase, deadline))
    if not rows:
        typer.echo("no claims")
        return
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))


@app.command()
def sh(name: Annotated[str | None, typer.Argument(help="claim name (default: newest)")] = None) -> None:
    """Shell into a claimed workspace."""
    _exec_shell(_bound_sandbox(name or _newest_claim_name()))


@app.command()
def extend(name: str, ttl: Annotated[str, typer.Argument(help="new lifetime from now")] = "24h") -> None:
    """Push a claim's shutdownTime out to now+TTL."""
    when = shutdown_time(ttl, datetime.now(UTC))
    patch = json.dumps({"spec": {"lifecycle": {"shutdownTime": when}}})
    _kubectl("patch", "sandboxclaim", name, "--type=merge", "-p", patch)
    typer.echo(f"{name} now dies {when}")


@app.command()
def rm(
    names: Annotated[list[str] | None, typer.Argument(help="claim names")] = None,
    all: Annotated[bool, typer.Option("--all", help="delete every claim")] = False,
) -> None:
    """Dispose workspaces (deletes the claim; sandbox + PVC follow)."""
    targets = [c["metadata"]["name"] for c in _claims()] if all else (names or [])
    if not targets:
        typer.echo("nothing to delete (name a claim or pass --all)", err=True)
        raise typer.Exit(1)
    _kubectl("delete", "sandboxclaim", *targets, capture=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
