"""Macro: ts_library — one Bazel target per TypeScript module, type-checked as it builds.

`js_library` only stages files; it never runs the compiler. A frontend built from it
therefore needs a second, whole-project `tsc` target that lists every file again, and the
two lists drift silently — a file added to the library graph but not to the checker's
inputs is simply never type-checked, and nothing fails.

`ts_library` removes the second list. Each target compiles its own `srcs` against its
deps' generated `.d.ts` in the same action that emits the `.js` its consumers bundle, so
the type check *is* the build step. Adding a file means adding it in one place.

Deviation from stock `ts_project`: the tsconfig is passed as a dict rather than a label,
so rules_ts writes a per-target config whose `files` array is exactly this target's
`srcs`. `tsconfig` here names the shared `ts_config` the generated config extends for
compiler options. Handing `ts_project` the shared file directly would instead let its
`include` glob pull in whatever else happens to be in the sandbox — every dep's emitted
declarations included — making each target's program the transitive closure.
"""

load("@aspect_rules_ts//ts:defs.bzl", "ts_project")

def ts_library(name, srcs, tsconfig, deps = [], **kwargs):
    """A TypeScript library: type-checks its own sources and emits `.js` + `.d.ts`.

    Args:
        name: Target name. Consumers depend on it exactly as they would a `js_library`.
        srcs: This target's `.ts`/`.tsx` sources. Becomes the generated tsconfig's
            `files` array, so it is the whole program — imports of other targets resolve
            through their `.d.ts`.
        tsconfig: Label of the shared `ts_config` holding the compiler options, which the
            generated per-target config extends.
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
        # tsc emits the .js and the .d.ts in the one action that type-checks them, so a
        # type error fails the build. A faster transpiler (swc, esbuild) would split
        # emitting from checking and let `bazel build` go green on code tsc rejects.
        transpiler = "tsc",
        declaration = True,
        source_map = True,
        resolve_json_module = True,
        **kwargs
    )
