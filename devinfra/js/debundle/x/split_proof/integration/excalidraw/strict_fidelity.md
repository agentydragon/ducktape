# Current Excalidraw strict-placement replay

Run `excalidraw_authored_placements` and `visible_elements_region` in this
package. The replay uses the pinned editor and main bytes from `excalidraw:bundle`
and the full Rust-authored schedules and selected ownership export. It validates
the original ordinals, resolved member names and actual evaluation order. The
diagnostic plan is a sidecar; it never writes JavaScript. The ordinary
`lowersplit --require all-outcomes` checks are separate.
[Measured replay and owner artifacts](https://app.buildbuddy.io/invocation/cad2e2ff-0988-42c9-b087-e2d2cf436852).

| Request                            | Original executable rows | Concrete / opaque fallback / refused | Lean structural conflicts |
| ---------------------------------- | -----------------------: | -----------------------------------: | ------------------------: |
| Full editor                        |                    1,781 |                       1,705 / 68 / 8 |                    60,295 |
| Full main                          |                    1,620 |                      1,566 / 37 / 17 |                   130,667 |
| Historical selected editor rows    |                      365 |                          364 / 1 / 0 |                    45,120 |
| Historical selected main row       |                        1 |                            1 / 0 / 0 |                     1,300 |
| Visible-elements source projection |                        6 |                            6 / 0 / 0 |                         0 |

The editor has 1,787 total ordinals, including five static import rows and
one standalone export row. Main has 1,628, including eight static import
rows. These metadata rows are excluded from the executable denominator.
The full authoring moves 403 editor rows to 37 nonentry owners, and all 1,628
main rows to 43 nonentry owners, including 19 generated residual groups.
Rust's full orders give 316,859 and 136,940 inversions respectively; those
are all Rust schedule inversions, whereas the table counts Lean footprint
conflicts only. For the historical subset the lowering columns count just
the selected rows, but the order check still runs on the entire chunk with
all unselected rows kept. The subset resolves to 365+1 distinct executable
ordinals and the same number of selected bindings on this pinned source.
The six-row projection is a separate denominator.

The empty-profile census lowers each executable row in context of the original
import names. A concrete success can still retain a deferred function body.
An opaque fallback here means the structural decoder could retain the row
when concrete lowering failed. Neither count establishes that the row runs
in an arbitrary caller, can move, or appears in a published split.
Artifacts include `owners.json` (per owner and placement), original ordinal
counts and first refusals in `census.json`, and the actual schedule sidecars
and checker logs for full and selected plans.

The strict checks expose independent limitations:

- **Lowering and constructor admission.** The authenticated classifier first
  refuses editor #41, `unsupported global Uint8Array`, and main #23,
  `unsupported linked expression MetaProperty`. The separate diagnostic
  move report uses a different input path and first stops at the eager calls
  at editor #5 and main #8; it does not enumerate failed semantic proof pairs
  on either full input. The ordinary CLI refuses both full requests without
  writing files.
- **Private cells and public exports.** On the intact sources, the full
  editor and main plans have 208 and 968 lexical cross-owner uses of original
  nonpublic bindings respectively; the selected plans have 138 and 1.
  Counts are distinct (binding, using owner) uses, excluding original imports
  and standalone publication rows. A lexical crossing is an obligation,
  not a proved mismatch or the number of glue exports the printer will emit.
  `links.json` lists the names, their defining and using owners, original
  public aliases and the counts per defining owner. In the six-row projection,
  the staged entry preserves the original public aliases `hc`, `kc` and
  `zj`, and its import of the private `st` predicate must resolve to the
  correct cell. The generated typechecks file exports `st` for that link;
  this says nothing about exposing the generated route to deployment clients.
- **Order.** Full main moves its eight static import rows outside the entry.
  Both full and both selected single-chunk plans still have structural order
  conflicts. The visible-elements plan keeps all six executable rows in
  source order, with four moving to the real typechecks owner. A control that
  reads `st` before initialization returns `undefined` in original order
  and `function` after moving the read past its initializer; the plan refuses.
- **Deployment.** Full editor would move three of its 13 location-sensitive
  sites; full main would move all 87 (59 dynamic imports and 28 metadata
  sites). The selected plans retain those sites in the original entries.
  Retention still leaves the static provider graph and lazy re-entry through
  the original module records. The normal CLI refuses the exact two-chunk
  manifest before selecting a constructor. The seven fixed static providers
  and the historical Chromium observation belong to the
  [retired replay](joint.md); they do not give a current production or browser
  certificate.

The closest region is the [visible-elements path](visible_elements_region.md),
with original editor ordinals 616, 617, 618 and 621 in typechecks, 1699 and
1745 in the entry, and three aliases from publication row 1786. It is 277
bytes of projected source, out of 517,159 original editor bytes. The checked
staging runner emits and independently rereads the files. Both CoreJS and
Node invoke the actual filter callback through a supplied ordinary receiver.
It discards the short free-draw element and returns the same rectangle
object in a fresh array. A supplied `isDeleted` getter throws an exact
sentinel while the first callback is running; the receiver's preceding
write survives and the second callback does not run. Rewiring the private
predicate admits the short free-draw element in Node and fails staged
readback. The actual native-array call still stops at
`Array.prototype.filter` in CoreJS. Ordinary publication still refuses
the projection, and no certified browser gate was enabled.
