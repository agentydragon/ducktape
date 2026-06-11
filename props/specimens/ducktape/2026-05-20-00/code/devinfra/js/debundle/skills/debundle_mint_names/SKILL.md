---
name: debundle_mint_names
description: >
  Assign descriptive names to unrenamed symbols in any debundle spec. Reads the
  emitted JS, understands each symbol's implementation and call sites, and
  updates the YAML spec `name:` field. Does NOT move modules or edit taxonomy.
  Trigger: user wants to name/rename minified symbols in a debundle RE spec.
---

# Debundle Mint Names

Assign descriptive names to unrenamed symbols in any debundle spec.

## Scope

You do exactly one thing: rename symbols whose YAML `name:` still equals
their `selector.binding.name` (the minified input-bundle name). Do **not**
move modules, redraw boundaries, edit taxonomy, or run cycle gates.

Allowed edits: changing the `name:` field of an existing member entry in a
`modules/**/*.yaml` file. Nothing else.

You **may** write a notes file (e.g. `module-structure-notes.md`) to record
observations about module organization you encounter while reading code:
symbols that belong together, modules that should be merged or renamed,
boundaries that could be redrawn. These are suggestions only — do **not**
apply any structural changes yourself (moving members, creating modules,
renaming module paths, reshuffling symbols between modules). Tell the user
about any suggestions you write so they can decide whether to apply them.

## Setup

Before starting, determine the debundle target. Look at the repo layout to
find the spec root — typically `<project>/re/<surface>/spec/<version>/`.
The debundle Bazel target follows the pattern
`//<path>/spec:debundle_<version>`.

```bash
# Mint a unique output base under /tmp to avoid lock contention
BAZEL_OUTPUT_BASE="/tmp/claude/debundle-mint-names/$(date +%s)"
mkdir -p "$BAZEL_OUTPUT_BASE"

# Build the debundle. Replace <target> with the actual label.
bazelisk --output_base="$BAZEL_OUTPUT_BASE" \
    build //path/to/spec:debundle_<version> \
    --config=nolint \
    --remote_download_outputs=all \
    --experimental_ui_max_stdouterr_bytes=20971520

# Resolve paths
BAZEL_BIN="$(bazelisk --output_base="$BAZEL_OUTPUT_BASE" info bazel-bin --config=nolint)"
DEBUNDLE_OUT="$BAZEL_BIN/path/to/spec/debundle_<version>.out"
```

Shell variables used throughout this skill:

| Var                  | Meaning                                                                       |
| -------------------- | ----------------------------------------------------------------------------- |
| `$BAZEL_OUTPUT_BASE` | Per-agent Bazel output base under `/tmp/claude/debundle-mint-names/<ts>/`     |
| `$DEBUNDLE_OUT`      | Debundle pipeline output root                                                 |
| `$SPEC_ROOT`         | Spec modules directory, e.g. `<project>/re/<surface>/spec/<version>/modules/` |
| `$UPSTREAM_JS`       | Raw upstream bundle JS (fallback when emitted tree unavailable)               |

The emitted JS tree under `$DEBUNDLE_OUT/app/` is the primary
reading surface: already-renamed companions show their descriptive names,
making unrenamed symbols stand out. Fall back to the upstream raw JS only
when the emitted tree is unavailable.

## How to find unrenamed symbols

### From the rename queue (preferred)

```bash
jq '.entries[:20]' "$DEBUNDLE_OUT/reports/rename_queue.json"
```

Each entry ranks by reference surface. Work top-down for highest impact.

### From the YAML spec (when queue unavailable)

```bash
cd "$SPEC_ROOT/.."
python3 -c "
import os, re
for root, dirs, files in os.walk('modules'):
  for fn in files:
    if not fn.endswith('.yaml'):
      continue
    path = os.path.join(root, fn)
    with open(path) as f:
      lines = f.readlines()
    i = 0
    while i < len(lines):
      m = re.match(r'^\s+- name:\s+(\S+)', lines[i])
      if not m or m.group(1).startswith('#'):
        i += 1; continue
      assigned = m.group(1)
      j = i + 1
      while j < min(i + 30, len(lines)):
        bm = re.match(r'^\s+name:\s+(\S+)', lines[j])
        if bm and 'selector' in ''.join(lines[i+1:j+1]):
          if assigned == bm.group(1):
            print(f'{path}:{i+1} {assigned}')
          break
        if re.match(r'^\s+- (name:|selector:)', lines[j]) and j > i + 1:
          break
        j += 1
      i += 1
" | sort -t: -k2 -n
```

Prioritize smallest modules first to build momentum. Then follow references
outward from each named module into its neighbors (see "Traversal strategy").

## Priority: named binds first, uncharted code second

Prefer naming symbols that already have a YAML binding (a member entry in
`modules/**/*.yaml`) but are still minified. These are already extracted
into the spec — they just need a descriptive name to complete the job.

When walking outward from a seed module, you will sometimes encounter code
in the emitted JS that has no module YAML binding yet (still residual code,
only covered by binding patches, or not attributed to any module). Don't go
out of your way to explore those areas, but **don't avoid them either** if
following a natural reference leads there. Reading that code to understand
the current symbol's context is legitimate, and if the meaning becomes
clear, note it for a future pass — just don't author new YAML entries for
it here (that's a module-planning or extraction pass).

## Traversal strategy: walk the code, don't jump randomly

The rename queue and YAML scanner give a flat list of unrenamed symbols.
Working through that list in arbitrary order wastes context: each symbol
requires reading surrounding code, and jumping from `aa` in one module to
`ZZ` in an unrelated module throws away everything you just learned.

Instead, **start from a seed and expand outward** — exactly like an
engineer reading unfamiliar code for the first time would:

1. **Pick a seed module** — smallest unrenamed module, or a module the user
   points at, or the top entry in the rename queue. Read the whole module's
   emitted JS file. You now understand that subsystem's vocabulary.

2. **Name everything in the module** — while the code is in front of you,
   name every unrenamed symbol in that YAML file. The bodies and call sites
   are already loaded; you're just writing down what you see.

3. **Follow references outward** — the module's imports and callers are
   now familiar terrain. When those imported/calling symbols are also
   unrenamed, name them next — you already understand the calling context.
   An `import { X } from "./helpers"` where you can see `X` is called as
   `getOptions(node)` in the code you just read takes seconds to resolve,
   because you already know what the caller does.

4. **Recurse into neighbors** — the helpers module you just reached
   probably has its own unrenamed symbols. Read it, name them, follow its
   references. You're doing a BFS/DFS over the call graph, not a random
   walk over an alphabetical list.

5. **Stop when the neighborhood is exhausted** — when you've reached
   modules where everything is already named, or where the code becomes
   opaque, pick a new seed in an unrelated area.

This produces **clusters of related renames** that make sense together,
rather than scattered individual renames. A reviewer reading the diff for
one module sees all its symbols named consistently, and each subsequent
module's renames build on names already established by its neighbors.

## The naming loop

For each unrenamed symbol:

### 1. Open the emitted JS file for the symbol's module

The YAML module path maps directly to an emitted JS file. For a module
at `modules/foo/bar/baz.yaml`, the emitted JS is at
`$DEBUNDLE_OUT/static/<chunk-id>/foo/bar/baz.js`. Open that file — the
symbol's definition is there, and its companions are already renamed to
descriptive names.

No grep needed: the module file IS the symbol's home. Just read it.

### 2. Read the implementation

Read 20-50 lines starting from the definition. Classify:

- **Function**: read the body, identify parameters, return value, side effects
- **Class**: read constructor, methods, static members, what it extends
- **Variable**: read the initializer — is it a factory? a constant? a config object?
- **React component**: look for JSX return, hooks, props destructuring

### 3. Read call sites

Check the emitted JS for callers — both within the module and in
importing modules. The imports at the top of each emitted file show
descriptive names for already-renamed symbols, so call sites read like
normal code. Follow import paths to read callers when needed.

Call sites reveal what arguments are passed, what the return value is
used for, and whether the symbol is a predicate, factory, formatter,
accessor, etc.

### 4. Assign the name

Edit the YAML spec file. Change only the member `name:` field:

```yaml
# Before:
- name: DZ
  selector:
    binding:
      kind: function_declaration
      name: DZ

# After:
- name: getAttributeOptions
  selector:
    binding:
      kind: function_declaration
      name: DZ
```

### 5. Verify (optional, at batch boundaries)

```bash
bazelisk --output_base="$BAZEL_OUTPUT_BASE" \
    build //path/to/spec:debundle_<version> --config=nolint
```

## Naming conventions

| Kind                 | Style                              | Examples                         |
| -------------------- | ---------------------------------- | -------------------------------- |
| Function (predicate) | `isX`, `hasX`, `canX`              | `isNodeEmpty`, `hasChildren`     |
| Function (accessor)  | `getX`, `findX`, `resolveX`        | `getNodeById`, `findParent`      |
| Function (mutation)  | `setX`, `updateX`, `applyX`        | `setSessionId`, `updateFilters`  |
| Function (factory)   | `createX`, `buildX`, `makeX`       | `createPalette`, `buildQuery`    |
| Function (format)    | `formatX`, `renderX`, `serializeX` | `formatError`, `serializeTree`   |
| Class                | `PascalCase` noun                  | `NodeAccessor`, `SessionManager` |
| Constant / config    | `camelCase` noun                   | `defaultTimeout`, `nodeIdLength` |
| React component      | `PascalCase`                       | `CommandButton`, `FilterPill`    |
| Type / interface     | `PascalCase` noun                  | `PanelState`, `SearchResult`     |

## Rules

- **Read before naming**: never rename a symbol without reading its body and
  at least one call site. A wrong name is worse than a minified name.
- **Don't guess**: if the body is opaque or the purpose is unclear after
  reading, leave the minified name. A later pass with more context can name it.
- **Name from behavior, not spelling**: the name should describe what the
  symbol _does_, not what the minifier happened to call it.
- **Keep names concise**: prefer `getNodeOptions` over
  `getOptionsForNodeAttributeDefinition`. Long names hurt readability too.
- **Respect existing naming in the module**: look at already-named siblings
  in the same YAML file for consistent style and terminology.
- **Batch by module**: when naming symbols in a file, name all unrenamed
  symbols in that file before moving to the next. Context accumulates.
- **Prefer smaller modules first**: fewer symbols means faster context
  buildup and easier review.

## Anti-patterns

- **Renaming without reading**: guessing from the binding name or adjacent
  symbols without understanding the implementation.
- **Overly generic names**: `processData`, `handleEvent`, `utils`,
  `helper`. These add no information over the minified name.
- **Names that encode the type**: `stringFunction`, `arrayHelper`.
  The type is visible in the code; the name should express the purpose.
- **Editing anything besides `name:`**: you do not move members, change
  selectors, add purity hints, or create new modules. Structural
  observations go in a notes file, not in the spec YAML.
- **Batching without building**: if you rename more than a handful of
  symbols, rebuild the debundle to verify before committing.
