"""`ts_library` and `vitest_test` for the session export frontend, with its shared `ts_config` filled in.

Both set `isolated_typecheck`: the shared `tsconfig.json` sets `isolatedDeclarations`, and rules_ts checks
that the two agree (see `ts_library` in <devinfra/js/ts_library.bzl>).
"""

load("//devinfra/js:ts_library.bzl", "ts_library")
load("//devinfra/js:vitest.bzl", "vitest_test")

_TSCONFIG = "//devinfra/claude/session_export/frontend:tsconfig"

def session_export_ts_library(name, srcs, **kwargs):
    """A `ts_library` of the frontend's shared `ts_config`.

    Args:
        name: The library target.
        srcs: The `.ts` and `.tsx` sources.
        **kwargs: As for `ts_library`, minus `tsconfig` and `isolated_typecheck`.
    """
    ts_library(
        name = name,
        srcs = srcs,
        tsconfig = _TSCONFIG,
        isolated_typecheck = True,
        **kwargs
    )

def session_export_vitest_test(name, srcs, **kwargs):
    """A `vitest_test` of the frontend's shared `vitest_config` and `ts_config`.

    Args:
        name: The test target.
        srcs: The spec files.
        **kwargs: As for `vitest_test`, minus `config`, `tsconfig` and `isolated_typecheck`.
    """
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//devinfra/claude/session_export/frontend:vitest_config",
        tsconfig = _TSCONFIG,
        isolated_typecheck = True,
        **kwargs
    )
