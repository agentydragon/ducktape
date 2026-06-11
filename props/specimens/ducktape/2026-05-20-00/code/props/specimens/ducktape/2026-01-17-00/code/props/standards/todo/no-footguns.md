# Property: No Footguns (Clear, Unambiguous Outputs)

**Status:** Planned
**Kind:** behavior

## Predicate

Outputs must be clear, correct, and unambiguous; when multiple accounting modes exist (e.g., first-match vs all-matches), the chosen mode must be explicitly surfaced in output/docs; avoid misleading displays (e.g., hard-coded extension lists diverging from constants).

## Acceptance Criteria (Draft)

- Chosen accounting mode is stated near the results (or in help/docs)
- Derived output from single source of truth (e.g., CODE_EXTS) — no drift between constants and displays
- Avoid confusing throwaway state that obscures meaning; inline when clearer
- When multiple modes are possible, make the chosen mode explicit in the output

## Examples Needed

- [ ] Negative: Hard-coded file extension list that differs from actual constant
- [ ] Positive: Output derived from single source of truth
- [ ] Negative: Ambiguous output where accounting mode is unclear
- [ ] Positive: Output explicitly states "showing first match" or "showing all matches"
