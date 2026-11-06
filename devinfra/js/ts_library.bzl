"""Compile each TypeScript module against its dependencies' generated `.d.ts`.

The target's `srcs` are also its complete source roster for type checking.

By default emit and check share an action. `isolated_typecheck` splits them: the build
only emits, and the non-manual `<name>_typecheck` target checks. `//...` and the
bazel-diff affected set built by <../ci/bazel_ci.py> include that target;
a bare `bazel build //pkg:lib` does not.

Deviation from stock `ts_project`: the tsconfig is passed as a dict rather than a label,
so rules_ts writes a per-target config whose `files` array is exactly this target's
`srcs`. `tsconfig` here names the shared `ts_config` the generated config extends for
compiler options. Handing `ts_project` the shared file directly would instead let its
`include` glob pull in whatever else happens to be in the sandbox — every dep's emitted
declarations included — making each target's program the transitive closure.
"""

load("@aspect_rules_ts//ts:defs.bzl", "ts_project")

def ts_library(name, srcs, tsconfig, isolated_typecheck = False, deps = [], **kwargs):
    """A TypeScript library: type-checks its own sources and emits `.js` + `.d.ts`.

    Args:
        name: Target name.
        srcs: This target's `.ts`/`.tsx` sources; imports resolve through deps' `.d.ts`.
        tsconfig: Label of the shared `ts_config` holding the compiler options, which the
            generated per-target config extends.
        isolated_typecheck: Split emit/check so consumer emits and checks can fan out.
            Opt-in per package: it requires
            `isolatedDeclarations` in the shared `ts_config` — rules_ts validates that the
            two agree — which in turn requires an explicit type annotation on every
            exported symbol in the package.
        deps: Targets providing `JsInfo` — other `ts_library` targets, `js_library`
            targets wrapping generated declarations, and `//:node_modules/*` packages.
        **kwargs: Passed through to `ts_project` (`assets`, `data`, `testonly`, `tags`, …).
    """
    ts_project(
        name = name,
        srcs = srcs,
        deps = deps,
        extends = tsconfig,
        # Empty include/exclude so the written `files` array is the entire program.
        tsconfig = {
            "exclude": [],
            "include": [],
        },
        # tsc, not a faster transpiler: swc and esbuild strip types without reading them,
        # so nothing would type-check these sources at all. `isolated_typecheck` splits the
        # actions while keeping tsc on both sides of the split.
        transpiler = "tsc",
        isolated_typecheck = isolated_typecheck,
        declaration = True,
        source_map = True,
        resolve_json_module = True,
        **kwargs
    )
