# Public split-proof scoreboard

## Snapshot 2026-11-28 — devel `27b68135d257d9ed34c358cf203eb36eefb77cd2`

### Attributed source files in the four readable bundles

The merged [source-ownership audit](library_splits/SOURCE_OWNERSHIP.md) joins
byte-verified source maps to the original unit spans. Its source-file homes
are evidence of file origin, not automatically proof of semantic module
recovery. Unknown, bundler-generated and fixture-entry units remain separate.
Span bytes count input units; whitespace and comments outside units are not
included. These are the four readable bundles from the first snapshot, not the
separate identity-baseline byte totals or the minified variants.

| Original input | Bytes / units   | Known single-file units / span bytes / homes | Generated glue units | Fixture / unmapped units |
| -------------- | --------------- | -------------------------------------------- | -------------------: | ------------------------ |
| Acorn          | 204,565 / 447   | 446 / 203,627 / 25                           |                    1 | 0 / 0                    |
| lodash         | 97,358 / 670    | 669 / 76,739 / 202                           |                    1 | 0 / 0                    |
| semver         | 78,583 / 59     | 47 / 72,651 / 47                             |                   10 | 1 / 1                    |
| Zod            | 767,238 / 1,072 | 1,048 / 716,800 / 90                         |                   23 | 1 / 0                    |

No unit has anchors in multiple files; a single-file classification does not
mean every byte is mapped. Acorn's homes include five upstream-generated data
files, separately from bundler glue. Zod's file attribution uses byte-verified
rebuilds of the published JavaScript from shipped TypeScript; it is not a
TypeScript column-level map. The audit leaves nonunique output AST matches and
rewrites unknown rather than guessing their owners.

Each readable bundle has two distinct requests on the same pinned bytes: the
previous halves and a source-file layout. Rust physically emitted both Acorn
layouts (2 and 26 files), both lodash layouts (2 and 203 files), and semver
halves (2 files); the five outputs matched their finite Node API smoke tests.
Rust refused semver's source-file request and both Zod requests. Strict
`all-outcomes` refused all eight without files, so **zero of these eight are
ordinary certified allocations**. The exact selectors, output-byte counts,
unknown matches and per-owner results are in the audit's
[machine-readable ledger](library_splits/results/source-ownership.json.gz)
and [replay](https://app.buildbuddy.io/invocation/cceda8fa-d96f-4a00-8782-b990203e1db4).

For example, among units with known source and output homes, lodash halves
co-locate 2,106 same-source pairs out of 111,556 output pairs; all 2,106
same-source input pairs stay together, but 109,450 different-source pairs are
also mixed. The source-file layout has 2,106 / 2,106 correct output pairs and
2,106 / 2,106 same-source input pairs. These are Rust observations, not strict
certificates, and they illustrate why units moved or byte equality alone do
not measure recovered logical groups.

The matching Acorn pair counts are 10,062 / 49,506 correct output pairs and
10,062 / 10,638 same-source input pairs for halves; the source-file layout has
10,597 / 10,597 and 10,597 / 10,638 respectively. One authored unit in that
layout is not uniquely located by the output matcher; that is an evidence gap,
not evidence that the unit was dropped. Semver halves have 0 / 522 correct
output pairs; each mapped input home contributes only one unit, so the
same-source pair denominator is empty. These pair counts exclude unknown output
matches and are not substitutes for the per-group member ledger.

### D3 BasisClosed: separately authored co-move now certified

The original prototype-only request from the previous snapshot remains
refused: it exposes a private constructor. The explicit alternative assigns
entry rows #2 and #3 to the helper and leaves #0, #1 and #4 in entry, changing
one of five row owners relative to that request. With a **caller-supplied**
Node ESM root and explicit format marker, the merged
[#20702](https://github.com/agentydragon/ducktape/pull/20702) now publishes
and independently rereads that alternative at `all-outcomes`. It preserves
the five original entry-row assignments of the alternative and the five
executable rows in two fixed providers. The two surviving constructor and
prototype members are together in the helper, with no unrelated original
source rows there; generated glue is separate. The original distribution's
legacy manifest does not itself supply the selected ESM premise.

Node exercises the actual public factory, prototype and fixed imported
function identities, drawing calls and the sentinel throw. The receipt's
client coverage remains unchecked and it makes no deployment or package-wide
claim. This result is one certified **alternative** placement, not a success
for the original prototype-only request or for the whole D3 package.
[Uncached devel replay](https://app.buildbuddy.io/invocation/8a370595-c3fa-46b8-8801-1d9fb059bed1).

## Snapshot 2026-11-28 — devel `aed42d64dd450748586df24c7996ad351cb74272`

The independently reproduced ordinary result remains **1/122**: one
certificate with all applicable saved observations passing and 121 refusals.
An [authored-placement census](https://github.com/agentydragon/ducktape/pull/20700)
at reviewed head `111009432f3f92488aa4e9cace018074dda60fd4` is under review;
it retains each original request and adds separately measured Rust-emitted
surfaces and per-unit diagnostics. Its [122-case observation](https://app.buildbuddy.io/invocation/6bb5c91a-f8d3-4cf3-ac25-561f96d83765)
found 44 cases where the actual Rust entry namespace differs from the original;
31 of those also change an exported function's own `name` descriptor. Rust
passes its authored expectations, which can intentionally request readable
aliases. These are strict surface differences, not 44 ordinary constructor
successes or 44 independent placement failures. The other 78 have no measured
namespace or name-descriptor difference; this observation does not establish
their equivalence.

The census's retained-suffix proof and real artifact readback for
`split_declarator_fragments` are conditional proof/test evidence. They do not
add an ordinary CLI admission, console semantics, a published artifact, or a
second certified member of the saved cohort. Its authored module-export and
whole-tree syntax observations still need ordinary runner coverage.

## Snapshot 2026-11-28 — devel `8dff388f2271f31ee30cef0eebbfaf191b0acd8c`

This update retains the prior snapshot below. The [full-bundle census](library_splits/results/full-blocker-census.json.gz)
has now landed with exact source spans, requests and outcomes; its 11 tested
placements remain separate from the historical diagnostic matrix. The
[saved 122 replay](https://app.buildbuddy.io/invocation/7ddea425-5c46-46c9-bbfe-876597820f8b)
reproduces the same per-ID ledger: one fully observed certificate and 121
refusals. No additional original allocation is inferred from those results.

### Unchanged artifact maps are not splits

The independently measured [identity baseline](excalidraw/identity_baseline.md)
covers ten pinned artifact maps: 104 files, 97 JS owners, 7,116,547 bytes and
10,012 source ordinals. These totals describe that baseline's exact files,
including its readable bundle bytes; they are not a sum of the earlier
whole-bundle split table. There are zero moved units, new owners, splitter
certificates, or native consumer checks for those maps. Fourteen configurations
in three CLI modes give 42 fail-closed requests; existing copies remain intact.
The [snapshot and controls](https://app.buildbuddy.io/invocation/c7b01e86-ca7a-4c6c-bba2-01f18473e405)
separately include one synthetic inline certificate with zero moves and one
synthetic two-unit, two-owner split with one moved unit, disk recheck and
Node result `7`. Neither is a pinned-workload split. Copying identical files
also leaves deployment observations such as module URL and instance identity
as separate obligations.

### Additional shipped D3 graph and Excalidraw boundary

The shipped [D3 BasisClosed graph](library_splits/D3_BASIS_CLOSED_GRAPH.md)
has one five-row entry and two fixed original modules; their input bytes are
not yet in this ledger. Original file routes are known; the helper ownership
is authored-only. Two distinct placements were tested. The requested
prototype-only move leaks the previously private constructor through an entry
export, a public behavior change. The explicit alternative moves the original
constructor too, changing one entry-row owner relative to that first request.
Both ordinary CLI requests refuse and publish nothing. The alternative's
separately staged test artifacts are reread and have a linked modeled proof and
Node drawing, identity and throw observations, but are not a CLI certificate.
[Replay](https://app.buildbuddy.io/invocation/9c02af35-3994-481a-ba96-cfaec4cc194e).

The newer [Excalidraw wrapper diagnosis](excalidraw/scroll_refusal.md)
traces the actual pinned provider and captures for the existing scroll requests.
The single and pair placements remain refused; no original allocation or
browser certificate is added. A reentrant throwing callback control observes
cached partial exports. The complete provider's first classifier refusal is
`Object.create` at #0; this is not a proof that later obligations are solved.
[Replay](https://app.buildbuddy.io/invocation/d047d580-ef84-454a-a906-85d6f11172ee).

### Recovery accounting

For a known upstream logical group, compare its surviving declarations,
associated prototype/method and initialization writes to output groups by
contents, independent of output filenames and printed text. Report members
recovered together, unrelated members mixed in, and members left residual or
split across files. Unknown and many-to-many origins stay separate; generated
glue is separate, and leaked private names or changed behavior are correctness
failures. Pairwise correct co-membership, false grouping and false separation
need their own denominators; neither one giant file nor singleton shards count
as recovering all groups. Source-span bytes are secondary to these group counts.
Input hashes and exact reread authenticate artifacts, not recovered module
fidelity. A byte-verified source-map attribution and the current tree-owner
report are in progress; their results will be added with their own version and
evidence when available.

## Snapshot 2026-11-28 — devel `d535b64e433b00100bde86154ce9e08eb2d59c39`

This is a versioned evidence ledger, not a claim about arbitrary programs or
all possible placements. Counts below are **distinct original requests**, not
repetitions, output files, negative controls, or the union of overlapping
projections. A later snapshot must retain its source revision and evidence.

A source unit is the frontend's top-level unit, including an export row and
one unit per declarator. Bytes are input bytes, not the size of the output.
“Authored-only” means the test authors supplied the ownership; it does not
recover an upstream module boundary. “Known” means an independently recorded
original owner, not just a chosen filename. Unknowns are left unknown. A CLI
refusal means no certified output for that request, not proof that all such
splits change behavior. Scratch/reference files and historical structural
certificates are reported separately from current ordinary CLI publication.
Disk means independent `--check-output` on the physical output. A Node or
browser observation covers only the listed consumer and inputs.

### Whole generated library bundles

The [pinned generator](library_splits/prepare.mjs) bundles the public package
entries with esbuild 0.28.2. These are complete generated inputs, not unchanged
upstream package files. Original upstream owners are **unknown**; each request
has two **authored-only** owners. The halves request places the first
`floor(units/2)` ordinals in `m0.mjs`, the rest in `m1.mjs`, in that priority.
The [eight-request replay](https://app.buildbuddy.io/invocation/8947f2d9-0045-4b17-90cc-6575355e05bc)
on this devel refused each original halves request without creating output.
Original public API smoke passed; there is no emitted output, disk pass, or
emitted consumer result. The [size/ordinal probe](https://app.buildbuddy.io/invocation/39f8b164-6450-4139-b718-b54967921eb3)
measures the same generated inputs. The historical diagnostic matrix is not a
constructor certificate; its archived Zod input had 268 units, not the 1,072
in this snapshot. A [machine-readable census](https://github.com/agentydragon/ducktape/pull/20680)
records exact selectors, offsets, outcomes and source hashes for these 11
requests; it is under review at this snapshot.

| Input, pinned package     |   Bytes | Original units | Original owners | Request: units / owners | Physically emitted / certified | First recorded boundary |
| ------------------------- | ------: | -------------: | --------------- | ----------------------- | ------------------------------ | ----------------------- |
| Acorn 8.19.0 readable     | 204,565 |            447 | Unknown         | 447 / 2                 | 0 / 0                          | `RegExp`, #8            |
| Acorn 8.19.0 minified     | 122,054 |            447 | Unknown         | 447 / 2                 | 0 / 0                          | See replay              |
| lodash-es 4.18.1 readable |  97,358 |            670 | Unknown         | 670 / 2                 | 0 / 0                          | `global`, #0            |
| lodash-es 4.18.1 minified |  29,903 |            670 | Unknown         | 670 / 2                 | 0 / 0                          | See replay              |
| semver 7.8.6 readable     |  78,583 |             59 | Unknown         | 59 / 2                  | 0 / 0                          | `Object`, #0            |
| semver 7.8.6 minified     |  29,296 |             59 | Unknown         | 59 / 2                  | 0 / 0                          | See replay              |
| Zod 4.6.7 readable        | 767,238 |          1,072 | Unknown         | 1,072 / 2               | 0 / 0                          | `Object`, #0            |
| Zod 4.6.7 minified        | 458,183 |          1,072 | Unknown         | 1,072 / 2               | 0 / 0                          | See replay              |

The generated input SHA-256 values are:

| Input           | SHA-256                                                            |
| --------------- | ------------------------------------------------------------------ |
| Acorn readable  | `e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee` |
| Acorn minified  | `254ed31d156a41e8a47bf6e7bb77d3437905261cdb49d0bfa8013f7ec7ab325a` |
| lodash readable | `00ba6a99a5abc67948346a25a7cb3064d730fd38b2256069f55a67b6c4f4fe02` |
| lodash minified | `2312c1079ede1f798572850477948ad8bbfd844fc828b2a647737297863cbe17` |
| semver readable | `9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44` |
| semver minified | `b5768a31853e8206b6c4750aa998c1818c3b5b79dc6774e7af0ac6be59e707b2` |
| Zod readable    | `5d67ee00c72ddfbaa8d82f88913330687b364e6767faf5065538238d21495538` |
| Zod minified    | `b807a87b3791427c8eb00d51d7b403458f22721257759b6e8f20ad5345226e9f` |

Three **additional** full readable requests, distinct from halves, also
refused without output: Acorn moves only #1, Acorn moves only #10, and semver
moves only #1. The entry retains all other units; each has two authored owners.
The [independent replay](https://app.buildbuddy.io/invocation/6a25843b-657b-409b-b366-b4c99217c131)
records exact ranges and hashes. These 11 whole-input requests do not include
any projected fixture below.

### Pinned package files and projections

| Exact input and origin                                                                                                                                                                                                                                           | Request and allocation                                                                                                                            | Emission, certificate and consumer evidence                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Lodash `isLength.js`](library_splits/LODASH_IS_LENGTH.md): complete shipped file, 800 bytes, 3 units; original package file known, new owner authored-only                                                                                                      | One positive: #0 in `limits.mjs`, #1–2 in entry; 3 units / 2 owners. Separate misplaced-export request.                                           | Positive publishes both files, certifies all 3 source units and disk readback; unchanged `isArrayLike.js` consumer observes boundary values and getter count. Misplaced export refuses. A separate snapshot/live-alias and reordered-snapshot control changes behavior and fails readback. [Evidence](https://app.buildbuddy.io/invocation/20db475f-86ec-4f8b-b4bc-df477132274e).   |
| [Lodash `_baseSortedUniq.js`](library_splits/LODASH_SORTED_UNIQ.md): complete 756-byte, 3-unit shipped entry; 797-byte fixed `eq.js`; original file known, helper owner authored-only                                                                            | One #1 helper extraction, #0 and #2 in entry; 3 units / 2 generated owners plus fixed provider.                                                   | CLI publishes and rereads the linked entry/provider under the owned-package route with a **separate two-statement forwarding caller**. The shipped `sortedUniq.js` caller is refused by that route; it is exercised in separate Node tests, not certified as that CLI context. Helper-existence discriminator changes false→true and is refused.                                    |
| [D3 `basis.js`](library_splits/D3_BASIS.md): complete shipped 1,436-byte, 4-unit file; original file known, helper owner authored-only                                                                                                                           | Two distinct requests: prototype #2 in helper; or public `point` #0 in helper. Both have 4 units / 2 owners.                                      | Prototype request publishes, certifies all 4 units at all outcomes, rereads and runs the unchanged 1,084-byte `bundle.js` caller, including writes before sentinel throw. Point move refuses and emits nothing. A modified helper changes behavior and readback refuses. [Replay](https://app.buildbuddy.io/invocation/6a25843b-657b-409b-b366-b4c99217c131).                       |
| [Zod v3 locale](library_splits/ZOD_V3_STATIC_GRAPH.md): complete original `en.js`, 4 rows, bytes not recorded here, within 10 original modules; known package routes, extraction owner authored-only                                                             | One authored placement: imports #0–1 and implementation #2 in helper, snapshot #3 in entry; 4 target rows / 2 target owners plus 9 fixed modules. | CLI refuses both publication and `--check-output` without modifying the reference tree. An independently assembled reference, not CLI output, matches 110 namespace keys and selected `zod/v3` consumer observations; wrong order throws and equal-shape wrong cell changes the error message. [Replay](https://app.buildbuddy.io/invocation/6a25843b-657b-409b-b366-b4c99217c131). |
| [Retrier 0.4.3](library_splits/retrier/README.md): complete original public ESM file, 10 units; byte count not recorded here; original file known, extraction owner authored-only                                                                                | One placement: #0–7 in helper, #8–9 in entry, original order; 10 / 2.                                                                             | CLI refuses fresh and reread; no certified output. A separately printed scratch tree is parsed and observed in Node: success 42 and a two-job rejection/FIFO trace agree. Timer/retry-acceptance path is untested. First linked decode blocker is `Math` at #7. [Replay](https://app.buildbuddy.io/invocation/6a25843b-657b-409b-b366-b4c99217c131).                                |
| [Acorn consumer projections](library_splits/WORKLOAD_COHORT.md#acorn-projections-that-call-original-exports): original lexical closures and partial export row from the 447-unit input; source bytes of projections not recorded here; halves ownership retained | Two distinct requests: `isNewLine` (#22) and identifier predicates (#0–3, #8–12), each with a partial #446 export row / 2 owners.                 | At the cited revisions, `isNewLine` emitted, certified and reread with six Node calls; identifier request refused at eager `RegExp`, without output. These are not the line-info requests below.                                                                                                                                                                                    |
| [Acorn line info](library_splits/ACORN_LINE_INFO.md): projection of 5 original body units and part of export row #446 from the 447-unit bundle; projection bytes not recorded here; owners authored-only                                                         | Three distinct layouts A, B, A+B, respectively 2, 2 and 3 owners; each requests the same 6 projected rows.                                        | All 3 publish and reread at all outcomes; Node checks exported line information and receiver/indices. A wrong cell and eager call controls expose changed behavior and refuse. Not a whole Acorn certificate.                                                                                                                                                                       |
| [Acorn node locations](library_splits/ACORN_NODE_LOCATION.md): 1,413-byte projection of 7 original body units and part of #446, from the 204,565-byte original; owners authored-only                                                                             | Two layouts: newline+scanner (3 owners) and newline+scanner+locations (4 owners), 8 projected rows each.                                          | Both requests publish and certify all 8 projected rows, reread and pass Node checks for constructor identity and locations. The older 0/2 result predates these rules. Wrong cell/order controls are distinct. [Replay](https://app.buildbuddy.io/invocation/b610d384-038a-4d26-ac0a-a8501e9b7213).                                                                                 |

### Saved and small cases

The [122 saved corpus placements](../debundle_cases/README.md) are individual
authored specs, not one program; source bytes, unit counts and owner totals
vary by row. The saved diagnostic baseline has 207 entries and selects 122
`agree-accept` requests without changing them. [Current six-shard ledger](https://app.buildbuddy.io/invocation/a9d998ed-7b90-4361-bf98-61b965acda69):
**1/122** emitted and certified, disk checked, and passed every applicable
authored observation; **121/122** refused without output. The winner is
`catchall_exports/unclaimed`: #0 in residual, #1 export row in entry, two
authored owners, from a 127-byte original with two source units. Both original
and emitted namespace are `foo` and `x-y`,
stdout is empty, and the saved probe prints `1 1`; disk checking covers live
cells. Wrong-name and equal-value wrong-cell controls are not extra successes.

The [two-job await fixture](../translation/linked/await_staged_link_test.mjs)
is a 348-byte, 10-unit authored-only small input, not recovered application
ownership. Its requested worker owns #0–4 and entry owns #5–9. The original
two-file placement publishes and rereads; the modeled immediate
turn and two job resumptions are proved, and Node observes `A7B7`. A swapped
schedule is a second, distinct request and refuses; a same-result disk mutation
is also refused. [Proof and CLI evidence](https://app.buildbuddy.io/invocation/efd496fa-be2e-4d7b-81c9-69008835a67d).
Other tiny examples have different source and requests; they must not be added
as recovered real-source allocations.

### Excalidraw and known upstream owners

The public pinned editor has 1,787 source units, SHA-256
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`;
its byte count is not recorded here. The authored full editor replay has 272
units outside entry and 35 nonentry owners; main has 1,628 outside entry and
43 nonentry owners. Those are [historical diagnostic replays](excalidraw/README.md#building-and-replaying-the-authored-excalidraw-specs),
not current whole-tree constructor certificates.

The independently resolved [owner-cluster ledger](excalidraw/audits/owner_clusters/README.md)
accounts for all 1,787 editor units, separating known, untested, refused and
unknown owners. Its historical 318-unit / 34-owner structural normal-completion
result had disk and Chromium observations; that proof route and browser gate
are retired and are not current ordinary constructor coverage. The two-unit
original scroll request on unchanged full editor bytes currently refuses and
emits no output. The [visible-elements projection](excalidraw/visible_elements_region.md)
keeps six executable original rows plus part of the export row in two owners;
its separate test runner stages and rereads a modeled initialization proof,
but the ordinary CLI refuses the projection. Its Node control and early-read
behavior difference are not a public browser certificate.

### Open accounting

Record the tree owner's current reviewed result and per-owner denominator when
available. Obtain missing whole-file byte counts and origin mappings rather than
inferring them from a projection. Track exact public-subpath and deployment
premises for each consumer, plus untested throws, mutation, identity and async
paths. A diagnostic acceptance, an independently assembled reference, a
readback refusal, and an emitted artifact are separate observations.
