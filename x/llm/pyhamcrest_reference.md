# PyHamcrest in repository tests

Use the [upstream documentation](https://pyhamcrest.readthedocs.io/en/latest/)
for the matcher API, custom matchers and test-framework integration.

Local assertion preferences:

- Prefer direct `assert` for exact values/structures, scalar comparisons, `None`
  and simple type checks. Exact equality should detect unexpected extra data.
- Use matchers for partial structures, nested conditions, approximate values or
  other flexible expectations; keep exact checks exact when the full result is known.
- Prefer keyword arguments to `has_entries` when keys are valid Python identifiers;
  use a dictionary for other keys.
- Use `has_item`/`has_items` for membership requirements. `contains_exactly` checks
  the entire sequence and its order; it does not allow unknown extra elements.
- Don't nest `assert_that` or pass it a boolean comparison in place of a matcher.
- Don't assert key/property existence immediately before indexing/accessing it:
  the substantive assertion already fails if it is absent.
- Keep custom matchers domain-specific; use their mismatch descriptions and the
  assertion's `reason` argument when extra failure context helps.
