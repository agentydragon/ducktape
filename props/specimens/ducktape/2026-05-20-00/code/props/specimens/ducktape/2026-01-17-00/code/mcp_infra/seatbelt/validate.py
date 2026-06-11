"""
Validators for SBPLPolicy that produce human-readable messages.

Layering contract:
- Read-only: do not mutate the policy.
- Pure analysis: return a list of messages; no error codes.
- Context is explicit and can be derived from the runtime via make_runtime_context().
"""

from __future__ import annotations

import platform
import shutil
from collections.abc import Iterable
from dataclasses import dataclass

from .model import Action, DefaultBehavior, FileOp, SBPLPolicy, Subpath


@dataclass(frozen=True)
class ValidationContext:
    macos_version: str | None = None  # e.g., "14.6.1"
    sandbox_exec_present: bool = False


def make_runtime_context() -> ValidationContext:
    ver, _, _ = platform.mac_ver()
    return ValidationContext(macos_version=ver or None, sandbox_exec_present=bool(shutil.which("sandbox-exec")))


def _file_read_subpaths(policy: SBPLPolicy) -> Iterable[str]:
    """Yield only directory roots allowed via (subpath "...") for file-read*.

    LiteralFilter entries allow the exact path only and do not confer recursive
    read access. For purposes of the default-deny sanity check below, only
    Subpath roots contribute coverage.
    """
    for fr in policy.files:
        if fr.op != FileOp.FILE_READ_STAR:
            continue
        for pf in fr.filters:
            if isinstance(pf, Subpath):
                yield pf.subpath


def validate(policy: SBPLPolicy, ctx: ValidationContext | None = None) -> list[str]:
    """
    Analyze a policy with an optional runtime-derived context and return messages.

    Messages are human-readable warnings/errors; callers decide severity.
    """
    ctx = ctx or make_runtime_context()
    msgs: list[str] = []

    # Platform visibility
    if ctx.macos_version is None:
        msgs.append("warning: non-macOS platform detected; seatbelt SBPL may be unsupported here")
    if not ctx.sandbox_exec_present:
        msgs.append("warning: sandbox-exec not found; cannot run SBPL via the deprecated CLI on this system")

    # Default-deny sanity for Python/dyld basics (heuristic, message-only)
    if policy.default_behavior == DefaultBehavior.DENY:
        read_paths = set(_file_read_subpaths(policy))
        required_roots = [
            "/System",
            "/usr/lib",
            "/private/var/db/dyld",
            "/System/Volumes/Preboot",
            "/System/Cryptexes",
            "/System/Volumes/Preboot/Cryptexes",
        ]
        # Coverage: a subpath root rp covers p when p == rp or p startswith rp
        # (i.e., p is inside rp). Do not treat rp being inside p as coverage.
        missing = [p for p in required_roots if not any(p == rp or p.startswith(rp) for rp in read_paths)]
        if missing:
            mv = ctx.macos_version or "unknown macOS"
            msgs.append(
                "warning: {} with default deny and no read access to {} — Python and the dynamic loader will likely fail; consider allowing file-read* of those roots and file-map-executable".format(
                    mv, ", ".join(missing)
                )
            )

    # Network note: if any non-local network allow is present, remind of egress risk
    for nr in policy.network:
        if nr.action == Action.ALLOW and not nr.local_only:
            msgs.append(
                f"note: network rule '{nr.op.value}' without local_only allows broader traffic; ensure this is intended"
            )

    # Mach lookup hygiene
    if policy.mach.global_names:
        names = ", ".join(policy.mach.global_names[:5])
        extra = "" if len(policy.mach.global_names) <= 5 else " (and more)"
        msgs.append(f"note: mach-lookup allows global services: {names}{extra}; verify necessity per service")

    return msgs
