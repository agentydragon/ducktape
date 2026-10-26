# Edit-gate claim projection controls

## Scope

Read-only investigation on devel `a0d30bd5d8dee557efd2d751d3a703587ee8aab2`.
The fixtures live under `/tmp/debundle-claim-controls-20261025-1`; no
production source, spec, or gate behavior was changed. Each command below used
the real CLI through `nix develop --command bb run //devinfra/js/debundle:debundle
-- ...`.

The initial tree used this config and source:

```yaml
# spec_config.yaml
main_chunk_id: main
inputs:
  root: source
  js_list_path: js-files.txt
unassigned_mode:
  main:
    kind: inline_in_entry
```

```text
# source/js-files.txt
main.js
```

```js
// source/main.js
const a = 1;
```

`vendor_marks.yaml` was `vendor_marks: []`; `modules/alpha.yaml` contained a
single member with `selector.binding.name: a`. The stale case added
`modules/stale.yaml` with a single direct member pin for `Ghost`.

The initial graph was produced with:

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- run \
  --tree-config /tmp/debundle-claim-controls-20261025-1/spec_config.yaml \
  --tree-modules /tmp/debundle-claim-controls-20261025-1/modules \
  --tree-vendor-marks /tmp/debundle-claim-controls-20261025-1/vendor_marks.yaml \
  --tree-source-root /tmp/debundle-claim-controls-20261025-1 \
  --out-root /tmp/debundle-claim-controls-20261025-1/out
```

BuildBuddy invocation `ca4f326b-10fb-45a0-849c-855a71886527` succeeded.

## Existing stale direct claim

The fixture source declares only `a` (`source/main.js` contains `const a = 1;`).
`modules/alpha.yaml` claims `a`; adding `modules/stale.yaml` with a direct
`binding.name: Ghost` pin reproduces a gap in the edit gate.

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- modules merge \
  --modules /tmp/debundle-claim-controls-20261025-1/modules \
  --graph /tmp/debundle-claim-controls-20261025-1/out/reports/tree/main/owner_graph.json \
  --target merged --dry-run stale --format json
```

First, merging `stale.yaml` itself returned `gate=passed` (invocation
`789a2569-7b42-4cd2-b721-c3187b436d5c`, exit 0), but that edit would remove
the bad claim, so it was not decisive on post-edit validation. The
discriminating command edits valid `alpha.yaml` and leaves `stale.yaml` in the
post-edit spec:

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- modules merge \
  --modules /tmp/debundle-claim-controls-20261025-1/modules \
  --graph /tmp/debundle-claim-controls-20261025-1/out/reports/tree/main/owner_graph.json \
  --target merged --dry-run alpha --format json
```

BuildBuddy invocation `15d4b42e-f596-4815-b036-d00e6009f669` exited 0 and
reported `"gate":"passed"`, planning to write `merged.yaml` and delete
`alpha.yaml`; `stale.yaml` remains. `--dry-run` made no changes. The real
pipeline then rejected the still-stale spec:

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- run \
  --tree-config /tmp/debundle-claim-controls-20261025-1/spec_config.yaml \
  --tree-modules /tmp/debundle-claim-controls-20261025-1/modules \
  --tree-vendor-marks /tmp/debundle-claim-controls-20261025-1/vendor_marks.yaml \
  --tree-source-root /tmp/debundle-claim-controls-20261025-1 \
  --out-root /tmp/debundle-claim-controls-20261025-1/out-stale
```

BuildBuddy invocation `9176e131-9130-4a44-bb86-0784aa4e5346` exited 1. The
pipeline reported that `stale` claims `Ghost` and no top-level declaration
with that name exists. Thus `gate=passed` on this edit path does not prove that
all direct binding pins in the post-edit spec resolve. The CLI docs
(`docs/cli.md`, “Validate-by-default” and “bindings assign”) describe the full
gate and validated post-edit spec without stating this narrower boundary.

## Valid import claim

The source was changed to:

```js
import { x as a } from "./vendor.js";
const b = 1;
console.log(a, b);
```

`vendor.js` exports `x`; `alpha.yaml` claims `a` with
`kind: import_specifier`; `beta.yaml` claims local binding `b`.

The full `run` command succeeded (BuildBuddy invocation
`d90ae865-1402-4753-9e86-d44bd0911157`, exit 0):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- run \
  --tree-config /tmp/debundle-claim-controls-20261025-1/spec_config.yaml \
  --tree-modules /tmp/debundle-claim-controls-20261025-1/modules-import \
  --tree-vendor-marks /tmp/debundle-claim-controls-20261025-1/vendor_marks.yaml \
  --tree-source-root /tmp/debundle-claim-controls-20261025-1 \
  --out-root /tmp/debundle-claim-controls-20261025-1/out-import
```

The graph has an import owner, a local declaration owner for `b`, and a
side-effect owner. The normal merge control below also returned `gate=passed` (invocation
`e83ed629-c85d-43c1-a737-e8e7c853b9b5`, exit 0):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- modules merge \
  --modules /tmp/debundle-claim-controls-20261025-1/modules-import \
  --graph /tmp/debundle-claim-controls-20261025-1/out-import/reports/tree/main/owner_graph.json \
  --target moved --dry-run alpha --format json
```

Moving only local owner `b` with `modules refine`, while preserving the import
claim, failed before editing (invocation `d0cdbd81-49e7-46b3-8293-e72128bb8b0e`,
exit 1):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- modules refine \
  --batch /tmp/debundle-claim-controls-20261025-1/refine.json \
  --graph /tmp/debundle-claim-controls-20261025-1/out-import/reports/tree/main/owner_graph.json \
  --modules /tmp/debundle-claim-controls-20261025-1/modules-import \
  --source-root /tmp/debundle-claim-controls-20261025-1/source --dry-run
```

The error was `module .../alpha.yaml claims binding "a", absent from
owner_graph.json`. `kind: import_specifier` is present in the parsed selector,
but this owner-assignment projection folds it into a bare binding-name set.
An import claim is a valid re-export claim, not a claim to a local declaration
owner for `a`.

## Legal repeated `var` declaration

`source-var/main.js` contains:

```js
var a = 1;
var a = a + 1;
```

`spec_config_var.yaml` uses `inputs.root: source-var`,
`inputs.js_list_path: js-files-var.txt`, and the same inline-entry mode as the
initial config. `js-files-var.txt` contains `main.js`; `modules-var/alpha.yaml`
has a direct binding-name pin for `a`.

The full pipeline command succeeded (invocation
`866f6cb4-c2b2-4995-b0b1-cae4aeb71f28`, exit 0):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- run \
  --tree-config /tmp/debundle-claim-controls-20261025-1/spec_config_var.yaml \
  --tree-modules /tmp/debundle-claim-controls-20261025-1/modules-var \
  --tree-vendor-marks /tmp/debundle-claim-controls-20261025-1/vendor_marks.yaml \
  --tree-source-root /tmp/debundle-claim-controls-20261025-1 \
  --out-root /tmp/debundle-claim-controls-20261025-1/out-var
```

Its owner graph represents both declaration sites and groups them in one
co-declaration atomic group.
Assigning the direct `a` pin to `gamma` failed because the edit gate mapped the
name to only the first owner, leaving the second in residual and splitting the
atomic group (invocation `a1be5ba9-9064-43cf-86ca-b6477be175cd`, exit 1):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- bindings assign \
  --modules /tmp/debundle-claim-controls-20261025-1/modules-var \
  --graph /tmp/debundle-claim-controls-20261025-1/out-var/reports/tree/main/owner_graph.json \
  --dry-run a:gamma --format json
```

The strict owner-refine projection also rejected the unchanged legal claim,
reporting that `owner_graph.json declares binding "a" in multiple owners`
(invocation `96a0ea95-ff26-4c62-9a3c-a4e01cb75ad0`, exit 1):

```sh
nix develop --command bb run //devinfra/js/debundle:debundle -- modules refine \
  --batch /tmp/debundle-claim-controls-20261025-1/refine-var.json \
  --graph /tmp/debundle-claim-controls-20261025-1/out-var/reports/tree/main/owner_graph.json \
  --modules /tmp/debundle-claim-controls-20261025-1/modules-var \
  --source-root /tmp/debundle-claim-controls-20261025-1/source --dry-run
```

That batch file contains:

```json
[
  { "owner_id": "owner:0", "module": "gamma" },
  { "owner_id": "owner:1", "module": "gamma" }
]
```

## Narrow fix contract

`MemberSelectorSpec::Binding` retains both the name and optional
`BindingSourceKind`; `SourceClaimSet` and the joint source resolver already
preserve typed selectors. The edit-gate assignment projections then discard
that distinction: one indexes the first `OwnerId` per name, while
`owner_module_assignments` requires each name to occur in only one owner.
There is no `BindingId` type in this devel snapshot; graph reports expose
declared names and owner IDs, while the in-memory graph retains SWC `Id`s.

The narrow seam is a shared typed projection from each binding claim to an
explicit claim kind and its resolved owner set. Keep `ImportSpecifier` claims
typed and route them through import/re-export validation without looking up a
local declaration owner. For local name pins, preserve all declaration-site
owners for the binding, so a legal repeated `var` claim assigns every site to
the requested module. Use that same result in the ordinary edit gate and the
strict complete-assignment projection; unmatched direct claims must be an
explicit unresolved outcome, never silently omitted. This fixes the observed
projection gaps without changing the pipeline's authoring contract.

## Projection control rerun

The projection change was then exercised from an isolated worktree based on
devel `b750df8eef37d4a40518bf37682ae726679b8355`, using the same temporary
fixtures and the real Bazel-built CLI. The import claim remained typed and did
not become a local owner claim; moving the local declaration passed
`modules refine` (invocation `33c312fd-6516-43a8-a9a4-902041b029b7`,
`gate=passed`, one owner reassigned). The repeated-`var` binding passed
`bindings assign` (invocation `72492662-44e7-4c9e-a68e-37b06cecac74`,
`gate=passed`) and moving both co-declaration owners through `modules refine`
passed (invocation `2896bc73-9a7a-4c52-a53c-3221f39ad3ba`, `gate=passed`,
two owners reassigned). All commands were dry runs. Strict rejection of the
stale direct claim is a separate follow-up change.

## Indexed projection rerun

After rebasing onto devel `98f571122f23ac3149573f234669f223eaabb751`, the
same dry-run controls passed with the per-projection name and co-declaration
index in place: import-aware refine (invocation
`7f5c3166-e08f-4d58-943c-cee00e74e3e2`), repeated-`var` assignment
(`1cfecf89-c5d2-490b-9777-60bdf3a961fa`), and repeated-`var` refine
(`cd0caa41-e3d0-47c6-8ee3-6afd142f5480`). Each reported `gate=passed`.

## Strict direct-claim checks

The follow-up uses parsed chunk evidence from the shared selector resolver for
typed direct pins. The resolver keeps source-order declaration ordinals and
applies the canonical kind-pin rule: at least one declaration site must match
the requested kind, and the first source-order site remains the representative.
It also distinguishes named imports from default and namespace imports. The
edit gate now rejects unresolved graph claims and typed source mismatches before
the realizability and atom-split checks. `--no-verify` still skips the edit
gate.

On the neutral fixture and rebased `devel` base `fbd4c5241e`, the four
existing focused CLI e2e targets passed (BuildBuddy invocation
`9b2f26d1-d2aa-4e29-9aaa-b65e9521bfba`). These real CLI dry-run controls then
reported against the same source snapshot:

- A valid named-import claim remained accepted while merging its local peer
  (invocation `6529dcec-8a42-4e57-b4d1-027dd4c76786`, `gate=passed`). The
  command supplied `--source-root` so the graph's relative source path could
  be read.
- A stale local direct claim was rejected because no owner matched it
  (invocation `75449ffd-8227-4a6e-980f-00d24cd2a765`).
- A stale `import_specifier` claim was rejected because no matching import
  existed in source (invocation `1d9be0ff-c1c3-49db-9ae3-53b0fdbbe42c`).
- A local binding pinned as `class_declaration` was rejected when source showed
  a variable declaration (invocation `2d6aec97-4bc3-42e0-80a9-a0a9eec3d254`).
- Repeated-`var` assignment still passed (invocation
  `786b0a22-13af-4596-a94f-4320cf522bb2`) and moving both co-declaration
  owners through `modules refine` still passed (invocation
  `8838c028-c8f1-4184-92cb-fee83bde3c93`, two owners reassigned).
- The same stale-import edit with `--no-verify --dry-run` reported
  `gate=skipped` (invocation `be5f8095-62af-475e-ae2e-cec669506252`).

Relative `source_location.source_path` values may need `--source-root` for
typed direct claim checks. Without it, an initial valid-import dry run could
not locate `main.js` (invocation `7b281375-8503-4f6e-b799-0c179ad3cbff`); the
rerun with the fixture's source root passed. No YAML was written by these
controls.
