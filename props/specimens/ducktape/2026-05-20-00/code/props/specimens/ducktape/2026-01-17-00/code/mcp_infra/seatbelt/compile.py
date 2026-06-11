"""
SBPL compiler: SBPLPolicy -> SBPL text.

Pure function: no mutations, no auto-inserted paths or platform probing.
"""

from __future__ import annotations

from collections.abc import Iterable

from .model import FileRule, LiteralFilter, NetworkRule, SBPLPolicy, Subpath


def _q(s: str) -> str:
    # Minimal quote for SBPL string literals
    # TODO(mpokorny): Verify SBPL quoting coverage (backslashes, quotes, non-ASCII/UTF-8, control chars). Add round-trip tests; extend escaping (e.g., parentheses?) if needed.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_path_filter(pf) -> str:
    if isinstance(pf, LiteralFilter):
        return f'(literal "{_q(pf.literal)}")'
    if isinstance(pf, Subpath):
        return f'(subpath "{_q(pf.subpath)}")'
    # Should be unreachable due to typing
    raise ValueError(f"unsupported PathFilter type: {type(pf).__name__}")


def _render_file_rule(fr: FileRule) -> Iterable[str]:
    # file-map-executable typically has no filters; if none, emit a bare allow/deny line.
    if not fr.filters:
        yield f"({fr.action.value} {fr.op.value})"
        return
    for pf in fr.filters:
        yield f"({fr.action.value} {fr.op.value} {_render_path_filter(pf)})"


def _render_network_rule(nr: NetworkRule) -> str:
    pred = " (local ip)" if nr.local_only else ""
    return f"({nr.action.value} {nr.op.value}{pred})"


def compile_sbpl(policy: SBPLPolicy) -> str:
    """Compile SBPLPolicy to SBPL text.

    No validation beyond shape typing; callers may run validators separately.
    """
    lines: list[str] = []

    # Header
    lines.append("(version 1)")
    lines.append(f"({policy.default_behavior.value} default)")

    # Trace
    if policy.trace.enabled and policy.trace.path:
        lines.append(f'(trace "{_q(policy.trace.path)}")')
        # Magic: ensure trace path is writable by the sandbox so the trace file can be created
        # TODO(mpokorny): This is implicit rule injection; consider making it explicit or configurable.
        lines.append(f'(allow file-write* (literal "{_q(policy.trace.path)}"))')

    # Process primitives
    if policy.process.allow_process_star:
        lines.append("(allow process*)")
    else:
        lines.append("(deny process*)")
    if policy.process.allow_signal_self:
        lines.append("(allow signal (target self))")
    else:
        lines.append("(deny signal (target self))")
    if policy.process.allow_signal_same_sandbox:
        lines.append("(allow signal (target same-sandbox))")
    if policy.process.allow_process_exec:
        lines.append("(allow process-exec)")
    if policy.process.allow_process_fork:
        lines.append("(allow process-fork)")
    if policy.process.allow_process_info_same_sandbox:
        lines.append("(allow process-info* (target same-sandbox))")

    # File rules (in given order)
    for fr in policy.files:
        lines.extend(_render_file_rule(fr))

    # Network rules
    for nr in policy.network:
        lines.append(_render_network_rule(nr))

    # System toggles
    if policy.system.system_socket:
        lines.append("(allow system-socket)")
    if policy.system.user_preference_read:
        lines.append("(allow user-preference-read)")
    if policy.system.ipc_posix_sem:
        lines.append("(allow ipc-posix-sem)")
    # sysctl-read: allow unrestricted if sysctl_read is True and no filters.
    # Otherwise, when names/prefixes provided, emit a filtered clause.
    if policy.system.sysctl_read and not policy.system.sysctl_names and not policy.system.sysctl_prefixes:
        lines.append("(allow sysctl-read)")
    elif policy.system.sysctl_names or policy.system.sysctl_prefixes:
        lines.append("(allow sysctl-read")
        for name in policy.system.sysctl_names:
            lines.append(f'  (sysctl-name "{_q(name)}")')
        for pfx in policy.system.sysctl_prefixes:
            lines.append(f'  (sysctl-name-prefix "{_q(pfx)}")')
        lines.append(")")

    # Mach lookup
    for name in policy.mach.global_names:
        lines.append(f'(allow mach-lookup (global-name "{_q(name)}"))')

    # IOKit open
    for io in policy.iokit:
        if not io.registry_entry_classes:
            lines.append(f"({io.action.value} iokit-open)")
        else:
            for cls in io.registry_entry_classes:
                lines.append(f'({io.action.value} iokit-open (iokit-registry-entry-class "{_q(cls)}"))')

    lines.append("")
    return "\n".join(lines)
