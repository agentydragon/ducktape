@README.md

Augur is pre-production. Do not add compatibility shims for older URL state
versions, request schemas, or serialized payloads unless the user explicitly
asks for backward compatibility.

When calling `ScenarioRun.{series,matrix,terminal}` or
`RolloutDetail.{series,terminal}`, always pass a `ReportMetric` enum member
(e.g. `ReportMetric.CASH_USD`), never a bare string. The signatures already
enforce this; documenting it here keeps the rule visible to new callers and
to LLM agents drafting tests.
