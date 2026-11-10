# External estimate comparisons

Research note, 2026-10-31.

## Comparison rules

Compare external estimates only after aligning the provider, exact model slug,
subscription plan, quota window, measurement date and token accounting. A family
label such as “Sol” is not enough to identify a model; preserve versioned names
such as “Sol 6.1”. Even matching model slugs do not make numbers comparable if
one is an API token-price calculation and the other is a subscription meter
estimate, or if cache, input/output mix or workload differs. Record those
differences rather than silently treating them as equivalent.

Keep measurements, assumptions and outside corroboration separate. A useful
external result can bound or challenge our estimate without being pooled into
our sample or treated as an independent replication unless its observations
and sampling unit are clear.

## All Done Sites Claude Usage Tracker

Sources:

- [Live tracker](https://alldonesites.com/claude-usage-tracker/)
- [Published JSON](https://alldonesites.com/data/claude-usage.json)
- [Public MIT-licensed source repository](https://github.com/jonathanavis96/claude-usage-tracker)
- [Probe history](https://github.com/jonathanavis96/claude-usage-tracker/blob/main/history/probes.jsonl)
- [Passive history](https://github.com/jonathanavis96/claude-usage-tracker/blob/main/history/passive.json)

The tracker is open source and publishes useful raw-ish observations. Its probe
history includes timestamps, model names, token counts by class, meter ticks,
and account labels; its passive history publishes daily aggregates and a token
class split. It does not publish the full private Claude transcripts. The site
describes pairing transcripts with each account's usage meter and reports both
five-hour and seven-day meter measurements.

As checked on 2026-10-31, the site reports 5.743B weekly tokens (5.496B–5.950B)
for Max 20x / Opus 5.5. Treat this as a workload- and reference-mix-dependent
estimate, not a universal pure-model cap: the site says token totals depend on
cache and token mix, and the repository README describes per-model token figures
as reference-mix scenarios. The displayed ranges describe observed spread, not
statistical confidence intervals. Current Pro and Max 5x weekly figures are
inferred from Max 20x.

The outside-contributor evidence is very thin: the live page reports one Max
20x contributor ID, and the published JSON has one stale sample with no complete
week. It does not independently validate a weekly estimate.

There is also a denominator discrepancy to resolve before relying on the main
weekly result as a four-account estimate. The live page says Max 20x weekly
figures are measured from four accounts, while the repository README says the
live plan's weekly windows come from one passive account. The page and raw JSON
were current on 2026-10-31, but the checked-in probe log's latest row is dated
2026-09-14 and the passive-history file says it was generated 2026-10-01. The
live summary is newer than those checked-in histories.

Assessment: the meter-plus-transcript method and public code make this a useful
cross-check, but the public contributor sample is not a replication, the
account denominator is inconsistent across the site and repository README, and
the exposed Git history lags the live summary. Use it as a separately sourced
estimate with these caveats; do not treat it as a population-level consensus.
