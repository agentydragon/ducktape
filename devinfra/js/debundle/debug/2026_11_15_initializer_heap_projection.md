# Initializer contracts consume qualified heap effects

Shared execution can identify a provider's `config.prefix` read exactly. Its
single-chunk projection cannot represent that foreign cell, so it conservatively
marks the projected heap external. The initializer-contract validator used this
projection even when the complete qualified heap ledger was available, rejecting
`private_metadata` for a stable provider configuration read.

The validator now consumes the qualified heap lane when available. Known cells
remain in the graph; the annotation covers neither their observed ordering nor
an `isolated` initializer's access to pre-existing state. An unavailable lane
still uses the conservative projection.

`initializer_source_only_test` covers accepted stable configuration, a known
provider-property write whose rejected emission changes `pa qb` to `pa pb`,
and refusal of an `isolated` initializer reading the same kind of provider
property. All 22 cases pass in
[19bccd2e](https://app.buildbuddy.io/invocation/19bccd2e-80ab-46dd-b0ee-f0cc4cdc282d).
That run precedes final schedule-guard integration; the integrated suite must
also pass before completion of [the plan](../plans/cross_chunk_parity.md).
