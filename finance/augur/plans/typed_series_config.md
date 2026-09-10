# Strong entity identities — deferred

The desired endpoint is distinct strongly typed IDs at domain/API boundaries:
a security identity must not be usable where a property identity is required.
Changing string prefixes alone does not provide that guarantee.

The [roadmap](roadmap.md) tracks this as IDTYPES, deferred. It is not a prerequisite
for OBSINPUT's typed conditioning records, INPUT's private lowering, or studies.
Existing typed keys and per-kind sampled frames should be reused rather than
replaced by another representation.

When a consumer makes this worthwhile:

- Identify the remaining untyped entity-ID arguments, fields and artifact keys.
- Use distinct nominal ID types and appropriate typed key unions; preserve
  kind information through observations, actions and results.
- Update affected producers, consumers and serialized contracts atomically.
  Decide boundary encoding from that concrete change, not a blanket prefix-renaming
  campaign or a compatibility shim.
- Keep downstream private artifact/config updates explicit. No new code needs
  a second supported format merely to keep an old deployment working.

OBSINPUT owns actual conditioning/provenance records and fallback-extractor
deletion. It may retain current factor encoding while removing raw-record
ambiguity; a global artifact/API/frontend ID migration is separate.
