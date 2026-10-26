# Debundle deferred architecture backlog

Unrelated follow-ups retained for a future decision. This is not the dispatch
queue; order active work in `TODO.md`. Remove an item when it is resolved or
rejected.

## A11 intrinsic integrity assumption

`docs/design.md` records A11 (that analyzed chunks run with unmodified built-in
prototypes) as an assumption. Prototype pollution can invalidate purity-whitelist
admission and is not detected. A top-level scan for assignments such as
`<Builtin>.prototype.<x> = ...` could check the in-corpus portion; pollution from
host code or other bundles remains an external assumption. Decide whether that
check is worth its cost before changing the runtime contract.

## Defensive comment in linker ordering

`graph/linker_order.rs::chunk_source_import_order_from_adjacency` has a
`None`-after-`Some` clause described as protection against future filter changes
that might admit non-constraining members. If the filter shape changes, test the
resulting invariant or remove the defensive branch.
