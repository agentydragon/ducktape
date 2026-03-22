@README.md

## Bazel Build

Type checking via `svelte_check_test`. ESLint via workspace lint aspect.

**Adding a new component/page/lib file**:

1. Create the `.svelte` or `.ts` file
2. Add a `js_library` target in the sub-package `BUILD.bazel`
3. Declare `deps` on exactly the files it imports
4. Add it as a `dep` of targets that import it

**Sub-package structure**:

| Package                    | Contains                                    |
| -------------------------- | ------------------------------------------- |
| `src/lib/`                 | Shared utilities, API client, stores, links |
| `src/components/`          | Reusable UI components                      |
| `src/components/stats/`    | Statistics-specific components              |
| `src/pages/`               | Page-level components                       |
| Parent (`props/frontend/`) | App entry, bundler, dev server, test infra  |

**Schema generation**: `:schema` target in `//props/frontend/src/lib` generates `api/schema.d.ts`.
