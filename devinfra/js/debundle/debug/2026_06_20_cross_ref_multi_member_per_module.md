# cross_ref: only one `cross_ref` member resolved per logical module — FIXED

**Status: fixed**, landed in #2398 (`28daa5f17`). Root cause and fix below; kept
as an RCA.

Found while stabilizing the gaffer tana/re `domains/graph/metaNode` spec
(2026-06-20). Three sibling re-export aliases in one module want `cross_ref`
`aliases:` selectors:

```
let kxe = Qg;  // NavigationStackAccessor_export   -> aliases NavigationStackAccessor
let HI  = UJ;  // NavigationStackItemAccessor_export -> aliases NavigationStackItemAccessor
let Wc  = FZ;  // calendarViewAccessor              -> aliases CalendarViewAccessor
```

Each anchor class (`NavigationStackAccessor`, …) lives in the **same module** and
is pinned by its own `source_match` (renamed to the readable name).

## Symptom

- Converting **one** of the three to `cross_ref { aliases: <ClassName> }` →
  pipeline builds, `regen_js_test` byte-identical, the alias resolves correctly.
- Converting **two or three** in the same module → all of them resolve to the
  `<unresolved>` sentinel and the build fails the duplicate-claim gate:

  ```
  logical_module static/index-DI2GynTv::domains/graph/metaNode has duplicate
  source binding claims:
  - source binding <unresolved> claimed 2 times:
    - export `calendarViewAccessor` (members[].selector.cross_ref as `calendarViewAccessor`)
    - export `NavigationStackItemAccessor_export` (members[].selector.cross_ref as `NavigationStackItemAccessor_export`)
  ```

  The member that resolves fine **alone** also failed once a second `cross_ref`
  was present.

## Root cause

The failing gate is `reject_duplicate_member_bindings` in
`lowering/exports.rs`, **not** anything in the cross-ref resolution path
(`resolved_anchor_bindings` / `resolve_and_claim_cross_refs`) the original
hypothesis pointed at. The failure happened _before_ cross-ref resolution ever
ran.

`reject_duplicate_member_bindings` groups a module's members by their `binding`
field and rejects any binding claimed by 2+ members. It ran at two points
(`plans.rs` request build, and `plan_builder.rs::add_explicit_request`, Stage A),
both **before** the post-Stage-A `resolve_and_claim_cross_refs` pass that fills a
`cross_ref` member's `binding` in.

Every post-Stage-A selector member (`cross_ref`, `reads_member`,
`member_of_module`, `passed_to_call`, `makes_decorate_call`) carries an **empty**
`binding` (`String::new()`) until its dedicated resolution pass runs — see the
`MemberRequest` field docs in `lowering/plans.rs` and `build_members`. The gate
skipped only `source_match.is_some()` members; it did **not** skip the other
empty-binding selector forms. So two `cross_ref` members in one module both
hashed under the empty string `""`, collapsed into a single group rendered as
`<unresolved>`, and tripped the "claimed 2 times" check. One alone never formed a
group (`len() == 1`), which is why single conversions worked.

## Fix

`reject_duplicate_member_bindings` now skips **any member whose `binding` is empty**
(not just `source_match`). Empty binding ⟺ "not yet resolved" for every selector
form, so this is exactly the set that must be deferred. Genuine duplicate claims
among these members are still caught later — `claim_post_stage_a_binding` checks
each resolved binding against `catalogue_index_by_name` / `bindings_catalogue` and
emits the same duplicate-claim diagnostic, now keyed on the _real_ binding instead
of the `<unresolved>` sentinel. Fail-closed behavior is unchanged: an unresolvable
cross-ref still errors in `resolve_cross_ref_member`, and a real duplicate of a
resolved binding (e.g. `cross_module_emission_test`'s `RuntimeCatalog` case) still
fails the gate.

The now-unreachable `<unresolved>` rendering branch in the gate was removed (no
empty binding can reach the duplicate report anymore).

Regression test:
`e2e/cross_ref_lowering_test.rs::cross_ref_aliases_multiple_in_one_module_resolve_to_distinct_bindings`
— two `cross_ref aliases:` members + two `source_match` anchors, all in one
module; asserts both aliases resolve to their distinct bindings (`A2`, `B2`) and
the tree runs under Node.

## Follow-up (gaffer, separate pass)

The same-module alias clusters left as honest name-pins with a `note:` blocker in
gaffer `metaNode.yaml` are now convertible to `cross_ref`. That's a separate
gaffer-repo pass; this note records only the ducktape-side fix.
