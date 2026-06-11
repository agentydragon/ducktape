from __future__ import annotations

import pytest

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from mcp_infra.seatbelt.runner import run_sandboxed_async
from mcp_infra.seatbelt.validate import make_runtime_context, validate

pytestmark = [*REQUIRES_SANDBOX_EXEC]

# This file documents observed dyld/startup behavior under seatbelt on this host.
# Key anchors and why:
# - /System/Library/dyld + Cryptex/Preboot variants: dyld shared cache lives here on 13-15
# - /usr/lib, /System/Library/Frameworks/PrivateFrameworks: install names and exec-mapped code
# - file-read-metadata on parents: dyld may compare inode/mtime (dylibsExpectedOnDisk)
# On this host, deny default + targeted allowances still abort with SIGABRT (-6) unless file-read* "/" is allowed.
# We keep these tests to capture and track the exact signature across OS updates.
#
# Apple OSS references (dyld + production sandbox profiles):
# - dyld cache format header (cryptexPrefixes, dylibsExpectedOnDisk):
#   https://github.com/apple-oss-distributions/dyld/blob/main/include/mach-o/dyld_cache_format.h
# - WebKit NetworkProcess sandbox (mac profile shows broad file-read* allowances and exec-mapped paths):
#   https://github.com/apple-oss-distributions/WebKit/blob/main/Source/WebKit/NetworkProcess/mac/com.apple.WebKit.NetworkProcess.sb.in
#
# Follow-ups / options to tighten (disproof plan):
# - Probe additional early-touch paths: /usr/share (locale/ICU/timezone), /System/Library/Preferences,
#   /private/var/db/timezone and any fs_usage-observed parents. Prefer read-metadata first; add read-data only when required.
# - Use sudo fs_usage -w -f pathname -t 3 echo (in one shell) + /bin/echo OK (in another) to capture actual paths;
#   grep for /(System|usr|private) to shortlist candidates. DYLD_PRINT_SEARCHING=1 may help outside sandbox.
# - Keep xfail with exact signature (SIGABRT -6, empty stderr/trace) per OS, and record when a specific allowance flips to success.
# - If minimal remains brittle across 13-15, recommend compromise: allow file-read* "/" while denying /Users and limiting writes.


def test_default_deny_without_dyld_roots_emits_warning():
    # Default-deny policy with only file-map-executable but no file-read* roots
    pol = SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[])],
    )
    msgs = validate(pol, make_runtime_context())
    # Expect a warning mentioning default deny and missing dyld/system roots
    assert any("default deny" in m and "/System" in m for m in msgs), msgs


@pytest.mark.xfail(
    reason="Document current abort signature for too-narrow default-deny; not stable across macOS versions",
    strict=False,
)
async def test_default_deny_narrow_policy_exec_aborts_or_fails():
    pol = SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[])],
    )
    res = await run_sandboxed_async(pol, ["/bin/echo", "OK"], trace=True)
    assert res.exit_code == -6
    assert not (res.stderr)
    assert not b""
    assert res.trace_path is not None
    assert (res.trace_text or "") == ""


@pytest.mark.xfail(
    reason="Even with explicit dyld roots, this host still aborts; will refine once stable", strict=False
)
async def test_default_deny_with_explicit_dyld_roots_succeeds():
    # Allow the known dyld/system roots explicitly
    # Known dyld/system roots to try explicitly (see dyld_cache_format.h cryptexPrefixes)
    roots = [
        "/System",  # umbrella for /System/Library/**
        "/usr/lib",  # loader + common libs
        "/private/var/db/dyld",  # shared cache database on some versions
        "/System/Volumes/Preboot",  # SSV overlays contain cache files
        "/System/Cryptexes",  # OS components delivered via cryptex
        "/System/Volumes/Preboot/Cryptexes",
    ]
    pol = SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath=r) for r in roots]),
        ],
    )
    res = await run_sandboxed_async(pol, ["/bin/echo", "OK"])
    assert res.exit_code == 0
    assert res.stdout == b"OK\n"
