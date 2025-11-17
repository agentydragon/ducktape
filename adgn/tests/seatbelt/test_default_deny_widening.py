from __future__ import annotations

import pytest

from adgn.seatbelt.model import DefaultBehavior, FileOp, FileRule, LiteralFilter, ProcessRule, SBPLPolicy, Subpath
from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]

# Incremental widening to document exactly which additions were insufficient on this host.
# A: map-only (deny default) → dyld missing cache/parents → SIGABRT
# B: + metadata on /, /System/Library, /usr/lib → still SIGABRT (metadata alone insufficient)
# C: + dyld cache subpaths (/System/Library/dyld and Cryptex variants per dyld docs) → still SIGABRT
# D: + file-read* /usr/lib (allow read of on-disk stubs) → still nonzero
# E: + file-read* /System/Library → still nonzero
# F: + file-read* /System (broad umbrella) → still nonzero here
# Conclusion on this host: only file-read* "/" stabilizes echo; narrower sets brittle across OS revs.
# References:
# - dyld cache format header (cryptexPrefixes, dylibsExpectedOnDisk):
#   https://github.com/apple-oss-distributions/dyld/blob/main/include/mach-o/dyld_cache_format.h
# - WebKit NetworkProcess sandbox (exec-mapped system paths and broad read allowances):
#   https://github.com/apple-oss-distributions/WebKit/blob/main/Source/WebKit/NetworkProcess/mac/com.apple.WebKit.NetworkProcess.sb.in


@pytest.mark.parametrize(
    ("label", "add_rules", "expect"),
    [
        (
            "A_map_only",
            lambda: [FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[])],
            {"exit": -6},  # SIGABRT on this host
        ),
        (
            "B_plus_metadata_root_and_parents",
            lambda: [
                FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
                FileRule(
                    op=FileOp.FILE_READ_METADATA,
                    filters=[
                        LiteralFilter(literal="/"),
                        Subpath(subpath="/System/Library"),
                        Subpath(subpath="/usr/lib"),
                    ],
                ),
            ],
            {"exit": -6},
        ),
        (
            "C_plus_dyld_cache_paths",
            lambda: [
                FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
                FileRule(
                    op=FileOp.FILE_READ_METADATA,
                    filters=[
                        LiteralFilter(literal="/"),
                        Subpath(subpath="/System/Library"),
                        Subpath(subpath="/usr/lib"),
                    ],
                ),
                FileRule(
                    op=FileOp.FILE_READ_STAR,
                    filters=[
                        Subpath(subpath="/System/Library/dyld"),
                        Subpath(subpath="/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/private/preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/System/Cryptexes/OS/System/Library/dyld"),
                    ],
                ),
            ],
            {"exit": -6},
        ),
        (
            "D_plus_read_usr_lib",
            lambda: [
                FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
                FileRule(
                    op=FileOp.FILE_READ_METADATA,
                    filters=[
                        LiteralFilter(literal="/"),
                        Subpath(subpath="/System/Library"),
                        Subpath(subpath="/usr/lib"),
                    ],
                ),
                FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/usr/lib")]),
                FileRule(
                    op=FileOp.FILE_READ_STAR,
                    filters=[
                        Subpath(subpath="/System/Library/dyld"),
                        Subpath(subpath="/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/private/preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/System/Cryptexes/OS/System/Library/dyld"),
                    ],
                ),
            ],
            {"exit": "nonzero"},  # expected still failing on this host, but record outcome
        ),
        (
            "E_plus_read_System_Library",
            lambda: [
                FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
                FileRule(
                    op=FileOp.FILE_READ_METADATA,
                    filters=[
                        LiteralFilter(literal="/"),
                        Subpath(subpath="/System/Library"),
                        Subpath(subpath="/usr/lib"),
                    ],
                ),
                FileRule(
                    op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/usr/lib"), Subpath(subpath="/System/Library")]
                ),
                FileRule(
                    op=FileOp.FILE_READ_STAR,
                    filters=[
                        Subpath(subpath="/System/Library/dyld"),
                        Subpath(subpath="/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/private/preboot/Cryptexes/OS/System/Library/dyld"),
                        Subpath(subpath="/System/Cryptexes/OS/System/Library/dyld"),
                    ],
                ),
            ],
            {"exit": "nonzero"},
        ),
        (
            "F_plus_read_System",
            lambda: [
                FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
                FileRule(
                    op=FileOp.FILE_READ_METADATA,
                    filters=[
                        LiteralFilter(literal="/"),
                        Subpath(subpath="/System/Library"),
                        Subpath(subpath="/usr/lib"),
                    ],
                ),
                FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/usr/lib"), Subpath(subpath="/System")]),
            ],
            {"exit": "nonzero"},
        ),
    ],
)
async def test_incremental_widening(label: str, add_rules, expect: dict):
    pol = SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=add_rules(),
    )
    res = await run_sandboxed_async(pol, ["/bin/echo", "OK"], trace=True)
    print(f"{label}: exit={res.exit_code} stderr_len={len(res.stderr or b'')} trace_len={len(res.trace_text or '')}")
    if expect["exit"] == -6:
        assert res.exit_code == -6
    elif expect["exit"] == "nonzero":
        assert res.exit_code != 0
    else:
        assert res.exit_code == expect["exit"]
