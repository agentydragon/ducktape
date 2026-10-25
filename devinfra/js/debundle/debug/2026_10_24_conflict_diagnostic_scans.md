# Repeated graph scans in rejection diagnostics

After the callback and value-set sharing changes, profiling the exact consumer
spec reached `compute_chunk_analysis end` but then spent a sustained sample
interval almost entirely in `sequenced_owner_causes`. That helper scanned every
owner edge to describe each conflict or sequencing edge. A collection of small
conflicts could therefore cost conflicts × graph edges; the cycle renderer also
repeated the scan even when it already held the relevant sequencing edge.

A neutral CLI reproduction declares independent pairs of mutable bindings,
with one deferred writer per pair. The spec intentionally assigns the two
bindings to different modules. Unrelated deferred reader functions add lazy
edges without joining the independent atomic groups. With 8,192 conflicts and
128 readers, 84% of sampled CPU was in `sequenced_owner_causes`. The 12,288-pair
case exceeded 60 seconds after main analysis completed on `c3dd94118c`.

The fix visits each requested owner's existing outgoing-edge adjacency instead
of the entire graph. Cycle diagnostics use the already known edge directly.
Both paths retain the impure-source filter, source locations, binding names,
verdicts and sorted owner order. No gate decisions or report schema change.

The CLI regression asserts that the independently conflicting module claims
are still reported. Existing purity-reason and cycle tests cover the diagnostic
contents. The test keeps the ordinary short timeout; it does not raise the
limit to accommodate the repeated scan.

The committed regression uses 6,656 pairs and 128 readers so it also fits the
default CI compilation mode. With the previous reporting helper, the exact
fixture timed out at 60 seconds in BuildBuddy invocation
`be2bb2fc-d8fd-4ef8-97ef-d261931b6bd2`. The indexed implementation passed the same fixture in 52.824 seconds in
`92530750-af69-4b39-8315-fde444fbe65a`, with the ordinary short timeout
unchanged.
