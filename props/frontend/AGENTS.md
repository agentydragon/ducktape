@README.md

## Bazel Build

Sub-packages use `js_library` directly (from `@aspect_rules_js`). ESLint linting is handled by the workspace lint aspect (runs by default); type checking by `svelte_check_test`.

**Dependency philosophy**: Declare precise deps. No grab-bag filegroups, no globs, no `_ALL_SRCS` constants. Each `js_library` target lists only the files it directly imports. Transitive deps propagate automatically via `JsInfo`.

**Adding a new component/page/lib file**:

1. Create the `.svelte` or `.ts` file
2. Add a `js_library` target in the appropriate sub-package `BUILD.bazel`
3. Declare `deps` on exactly the local files it imports (other components, lib utilities)
4. Add it as a `dep` of the target(s) that import it (a page, the app entry, etc.)
5. Do NOT add it to any grab-bag list or constant — transitive deps handle propagation

**Sub-package structure**:

| Package                    | Contains                                              |
| -------------------------- | ----------------------------------------------------- |
| `src/lib/`                 | Shared utilities, API client, stores, link components |
| `src/components/`          | Reusable UI components                                |
| `src/components/stats/`    | Statistics-specific components                        |
| `src/pages/`               | Page-level components                                 |
| Parent (`props/frontend/`) | App entry point, bundler, dev server, test infra      |

**Schema generation**: The `:schema` target in `//props/frontend/src/lib` wraps a `js_run_binary` that generates `api/schema.d.ts`. The binary and OpenAPI input genrule stay in the parent package.
