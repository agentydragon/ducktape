# Unchanged artifacts and identity placement

You can keep an existing deployment in place regardless of which source features the
splitter understands. That operation moves no unit, introduces no route, and does not
load the application again. A byte-for-byte copy with the same relative paths is also
possible for a readable tree of regular files, but deploying the copy somewhere else
is a different operation. Neither operation establishes that the original deployment
links, completes normally, or behaves as intended.

## Reproduce the baseline

```sh
p=//devinfra/js/debundle/x/split_proof/integration/excalidraw
bbr test "$p:identity_baseline" --test_output=all --remote_build_event_upload=all
```

The test compares every selected original file with the copy as raw bytes and compares
the complete relative file set and the designated entry. The test output archive
contains `identity-baseline.json` with each file's path, size, source-unit count and
SHA-256, and the CLI verdicts. Source units use `topLevelStatements`: count imports
and export rows and give each top-level variable declarator its own ordinal. These
are not CoreJS executable-unit counts.

| Original input                                       | Unchanged files (JS owners) |             Bytes | Source units |
| ---------------------------------------------------- | --------------------------: | ----------------: | -----------: |
| Excalidraw 0.18.1 production esbuild tree, `main.js` |                     84 (79) |         5,162,924 |        5,312 |
| Acorn 8.19.0, minified / readable                    |                  1 (1) each | 122,054 / 204,594 |     447 each |
| lodash-es 4.18.1, minified / readable                |                  1 (1) each |  29,903 / 103,216 |     670 each |
| semver 7.8.6, minified / readable                    |                  1 (1) each |   29,296 / 81,371 |      59 each |
| Zod 4.6.7, minified / readable                       |                  1 (1) each | 458,183 / 770,084 |   1,072 each |
| Published Zod 4.6.7, static closure of `v3/index.js` |                     12 (10) |           154,922 |          204 |

That is **10 artifact maps, zero moved units, zero new owners and zero splitter
certificates**. Excalidraw includes all five non-JS files in esbuild's output
directory. The serving HTML, origin, and browser environment are outside that
directory. The original source retains browser names, `import.meta` and the bare
dynamic import of `@excalidraw/mermaid-to-excalidraw`. The eight library bundles
come from [the pinned preparation target](../library_splits/prepare.mjs); the
published Zod case uses the raw files and both package manifests. Neither the
Excalidraw app nor the pinned library workloads run in this test.

## Ordinary CLI

At the baseline revision the CLI has no generic byte-preserving route. The
table shows the result of three calls per configuration: publish to a missing
path, publish over an existing copy, and `--check-output` against that copy.

| Configurations                                                                                    | Exact reason                                                    |
| ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Excalidraw and published Zod v3, all JS chunks fixed                                              | `no constructor supports multi-chunk deployments`               |
| Eight singleton library bundles, fixed                                                            | `no constructor accepts the requested placement of <filename>`  |
| Authored all-in-entry Excalidraw `main.js`, Acorn `acorn.mjs`, and the native control `entry.mjs` | `no constructor accepts the requested placement of <filename>`  |
| Excalidraw with `--host-contract browser-page`                                                    | `no constructor supports host or structural deployment options` |

All **42** calls exit 1 and return `status: rejected`, `reason_code:
no_constructed_rule`, `scope: null`, `guarantee: null` and
`every_ending_matches: false`. The fresh output path is absent afterward. Both
calls with an existing destination leave it untouched. The CLI defaults to
`normal-completion`; the authored Excalidraw main request explicitly asks for
`all-outcomes`. The [certification dispatch](../../EsmSplit/LowerSplit/Certified.lean)
rejects host and structural options before trying constructors; a fixed
singleton cannot take an authored route, and a fixed multi-chunk request
does not meet the authored entry requirement of the finite static graph route.
A pre-existing byte match does not bypass construction in `--check-output`.

Two synthetic controls distinguish acceptance from transformation. A supported
authored inline `export const` certifies and passes readback with the same route
and owner but different whitespace bytes after printing. A separate two-unit
request actually moves one unit to `worker.mjs`, changes the entry bytes,
certifies at `all-outcomes`, passes disk readback, and returns the expected
export in Node. Only the second control counts as a split.

## Proposed rule: verify an unchanged artifact map

Provide a separately requested, read-only operation. For a selected snapshot,
compare the exact designated entry, normalized relative file routes, and raw
bytes of every file. Require the complete selected file set, including assets
and configuration files in scope. Use immutable snapshots or verify under a
stable read; reject missing or extra files, path aliases, symlinks, type changes
and changes detected during capture. A hash is useful in the receipt for
reproduction; the comparison checks bytes. Do not convert an unaccepted
placement into an unchanged result: if an operation also accepts a placement,
it must validate that every original ordinal retains its owner and position,
and there are no new owners, rewritten exports, or generated files.

Issue an `unchanged-artifacts` receipt with the entry, scope and file list,
zero moved units and new owners, and no CoreJS or native behavior guarantee.
A check over files can prove equality of the two finite artifact maps. Any
observation defined only on that map has equal results by substitution. This
needs neither a syntax restriction nor a host profile. It does not prove
successful linking or a public live-cell claim: for example, CoreJS's
[`SameExports`](../../EsmSplit/CoreJS/LinkedBehavior.lean) requires a successful
export resolution on both sides. Reflexivity of modeled execution, even if
lowering succeeds, adds no evidence of a split.

For deployment without host or loader premises, select the already deployed
original in place. Make no file, routing, loader, cache, or execution changes.
For a separate candidate deployment, the artifact receipt alone cannot
establish matching module URLs and formats, the live dependency instances
obtained from resolution, or loader, host and client behavior. Leave any such
claim unissued. Independently executed copies are not promised to produce the
same observations even if they start from the same source files.

The controls reject a renamed entry with identical bytes, an added file, and a
renamed public export; Node observes the changed namespace keys in the last
case. More strongly, both complete copies of a synthetic tree have the same
relative entry, file set, bytes and bare import text. Node nevertheless
observes different `import.meta.url` strings and different live object values
from the separately resolved provider modules. Each entry's export aliases the
provider's object in its own deployment. Those observations rule out inferring
native equivalence or external dependency identity from the artifact receipt.

A nontrivial split still needs a constructor that proves the requested
placement, binding and export correspondence, and the requested CoreJS
outcomes, plus checks tying that proof to the actual emitted and reread files.
A deployment claim additionally needs evidence for the real routes, formats,
resolution and module instances, and any host observations the transformation
might change. A no-op receipt supplies none of these transformation or
deployment proofs.
