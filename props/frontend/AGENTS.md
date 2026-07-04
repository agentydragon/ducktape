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

## `@tabler/icons-react` imports (preventive)

**Deviation** from the stock barrel import: `@tabler/icons-react`'s barrel
(`import { IconX } from "@tabler/icons-react"`) makes esbuild peak ~8.7 GB and
OOMs the default RBE VM — there is no per-action memory lever, only a blunt
platform-global override that hurts CI concurrency. This bundle uses the same
`spa_bundle` esbuild path, so if you add `@tabler` icons, import each by subpath
(`@tabler/icons-react/dist/esm/icons/IconX.mjs`, default export) and add an
ambient `declare module` for `tsc`. Full RCA:
<../../haku/console/debug/esbuild_tabler_memory.md>.
