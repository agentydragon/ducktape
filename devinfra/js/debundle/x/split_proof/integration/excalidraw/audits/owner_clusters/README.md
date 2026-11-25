# Editor owner-cluster retries

Adding `packages/excalidraw/change::Delta` (`Ie`, source unit 1462) to
[the 317-binding editor placement](https://github.com/agentydragon/ducktape/pull/19819)
certified **318 units across 34 owners**. This places the class in its canonical
change module while retaining `DEFAULT_FONT_FAMILY` in constants. In the
[combined RBE replay](https://app.buildbuddy.io/invocation/ba8387f0-0d37-4c9e-9e9a-b9496569e86d),
certified emission and independent disk checking agreed on structural normal
completion. Chromium exercised the original and overlaid editors and compared all
611 public export names. The source hash was
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`.
Both runs used the browser-page contract with stable `Math.PI@1` and the actual test origin.

## Candidate selection and observed limits

The initial bank used the [316-binding selection](https://github.com/agentydragon/ducktape/pull/19814)
with the checker at `425b6827`, including the own numeric prefix-read and fresh Symbol rules.
We selected closures over known private dependencies, leaves with few capture edges, and
initializers for which the new rules might matter. For example, `Snapshot` can read
`getObservedAppState`; moving both tests a different placement than the earlier attempt
that left the reader in entry. A prior multi-class trial stopped at an extra entry export
before isolating whether `Delta` could move.

| Addition to 316                                                            | First result                                                                                                                                 |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `Delta`                                                                    | Certified alone. The initial test timed out without a verdict; an isolated rerun at the same timeout passed emit, disk recheck and Chromium. |
| `ORIG_ID`, `__test__defineOrigId`                                          | Unit 472 (`ORIG_ID`) did not pass the local allocating declaration check. Modeling Symbol identity did not certify this move.                |
| `getObservedAppState`, `Snapshot`                                          | The `AppStateChange` class (1463) can access the moved reader (1469).                                                                        |
| Text-wrap caches, their writers, regex tables and `parseTokens` (13 units) | `getLineBreakRegex` (657) can access the region of `getLineBreakRegexSimple` (662).                                                          |
| `getUncroppedWidthAndHeight`                                               | `cropElement` (1185) can access its region (1188).                                                                                           |
| `round`, `createPointSnapLines`, `dedupeGapSnapLines`                      | `getElementsCorners` (1368) can access the moved `round` (1375).                                                                             |
| `getNextMultipleWidthAndHeightFromPointer`                                 | Unit 1724 can access its region (1736).                                                                                                      |
| `dataURLToString`, `base64ToString`                                        | Unit 1548 can access the moved `base64ToString` (1780).                                                                                      |
| `copyTextViaExecCommand`                                                   | `copyTextToSystemClipboard` (1634) can access its region (1635).                                                                             |
| CJK cache, writer and tables (4 units)                                     | `containsCJK` (656) can access the moved `CJK` table (660).                                                                                  |
| `COLOR_PALETTE`, `DEFAULT_ELEMENT_PROPS`, `_newElementBase`                | The move of `COLOR_PALETTE` (367) fails the local allocation check before the checker can use a property-read proof.                         |

[First nine proposals](https://app.buildbuddy.io/invocation/d8696c96-4878-49f2-8db6-cb991dd638bb)
· [Two follow-ups and the timed-out singleton's rerun](https://app.buildbuddy.io/invocation/6d98b177-200a-4079-8dfa-360e85600d0b).
Each terminal refusal is the verdict on that proposal as a whole. The test harness
checked for absence of published output after a refusal.

## Accounting and reproduction

| Selected baseline | Units in baseline | Added in verified conjunction | Only in refused trials | Known owner, untested | Unknown source owner |
| ----------------- | ----------------: | ----------------------------: | ---------------------: | --------------------: | -------------------: |
| 316               |               316 |                             1 |                     31 |                    98 |                1,341 |
| 317               |               317 |                             1 |                      0 |                   129 |                1,340 |

[Counts by owner at 316](per-owner316.tsv) include the three earlier heading proposals
under their original evidence links. [Counts by owner at 317](per-owner317.tsv) include
only the new combined trial. Each row set accounts for 1,787 units; neither counts a
refused group's members as individually impossible. The 317 tree resolves one more
canonical name than the 316 tree: `DEFAULT_FONT_FAMILY` (#409).

To inspect every unit or reproduce a proposal, build the paired Rust exports on the
pinned input revision, as in the [inventory instructions](../editor_306/README.md).
Use [trials316.json](trials316.json) with the [316 pair](https://app.buildbuddy.io/invocation/0c8e1c3b-8694-4c44-8bce-df1ccc8b40fe)
or [trials317.json](trials317.json) with the [317 pair](https://app.buildbuddy.io/invocation/2b34b690-42ee-4cb6-8881-f164023b32ed).
For the latter, at `c6a64c48`:

```sh
python3 ../editor_306/inventory.py canonical_editor_placement.json selected_editor_placement.json \
  --record trials317.json --trial default_delta > resolved318.json
```

Omit `--trial` for the per-unit ledger. For a fresh proof, pass the resulting placement
through the [deployment manifest](../editor_306/README.md), using the same host
contract and origin. Run `lowersplit`, then repeat with `--check-output`.
