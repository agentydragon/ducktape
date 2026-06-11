# Property: Prefer Comprehensions for Simple Filter/Map

**Status:** Planned
**Kind:** outcome

## Predicate

For simple, readable cases, prefer list/set/dict comprehensions (and generator expressions) over loops that only append/continue or build trivial maps.

## Acceptance Criteria (Draft)

- Single-predicate filters and simple key/value mapping use comprehensions
- No multi-branch loop when a single comprehension with a concise predicate fits on one readable line
- Fall back to loops when readability would suffer (long, nested conditions)
- Keep pre-checks (e.g., early returns) outside the comprehension for clarity

## Examples Needed

- [ ] Positive: Simple filter using comprehension
- [ ] Positive: Simple map using comprehension
- [ ] Negative: Loop with single append
- [ ] Acceptable: Complex loop with multiple branches (comprehension would hurt readability)
