# Scan: Duplicated Code Patterns

@../shared_context.md

Find repeated implementations that should evolve together. Prioritize test setup,
fixtures, and workflows, then production logic, transformations, and configuration.
Favor local clarity when an abstraction would hide the behavior under test.

## What counts as a finding

Three or more occurrences are a useful starting point. Extraction is also worth
considering for complex, error-prone logic that must change consistently. Compare
callers before grouping them: similar syntax alone does not establish a shared
contract, and API versions may need to evolve independently.

| Pattern               | Look for                                                        | Candidate change                                                                     |
| --------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Fixtures              | The same entity factory or fixture in multiple modules          | Use the nearest shared `conftest.py`; keep test-specific values explicit.            |
| Setup and teardown    | Repeated database, filesystem, or service lifetimes             | Share a fixture that preserves isolation, scope, and cleanup.                        |
| Matchers              | Repeated domain-specific response or object validation          | Reuse a matcher or assertion helper while keeping expected values at the callsite.   |
| Mocks                 | Repeated configuration of the same service/client/I/O boundary  | Share a configured mock fixture; do not replace real data models with mocks.         |
| Parameter data        | The same meaningful input cases copied between modules          | Share the case data when it represents one contract; retain distinct error paths.    |
| Assertion sequences   | Repeated checks of one operation's result                       | Share the common checks without hiding differences such as status codes.             |
| Structured test data  | Large repeated TypeScript/Vitest objects or resource envelopes  | Use a typed factory with overrides; keep discriminating values visible in each test. |
| Interaction workflows | Repeated render, readiness, click, and dialog-opening sequences | Share an async helper that awaits readiness and preserves interaction order.         |

For production code, apply the same contract test to business rules, validation,
transformations, utility functions, and configuration setup/teardown. Search for
an existing implementation before proposing another helper.

## Required detection pass

Run a clone detector over the entire requested tree before manual review. `jscpd`
can cover multiple languages; PMD CPD is another option. Start with small blocks
(about five lines or 30 tokens) for high recall. Record the command and scope, and
review **every candidate**, including low-confidence matches; tool similarity is
not evidence that the implementations should be merged.

Supplement the clone report with searches for:

- Repeated fixture and assertion-helper definitions.
- Common stripped lines and repeated assertion sequences.
- Identical mock targets, return-value setup, and parameter lists.
- Setup/teardown and data transformations with different local names.

AST comparison can expose structurally similar Python functions with different
names. Compare both structure and semantics: normalization that erases identifiers
can also erase the distinction that makes two tests valuable. Group findings by
shared behavior rather than reporting every matching pair separately.

For each candidate, inspect all occurrences and assess:

1. **Recurrence and complexity:** how often does it appear, and is the shared part
   substantial enough to justify an abstraction?
2. **Consistency:** must changes propagate to every occurrence, or can they diverge?
3. **Scope:** which package owns the behavior, and which existing helper could own it?
4. **Coverage:** which inputs, branches, assertions, snapshots, and resource-lifetime
   boundaries distinguish the callers?
5. **Net cost:** does the extraction reduce maintenance and code after accounting
   for the helper, imports, and build dependencies?

## Extraction boundaries

Put shared pytest fixtures in the nearest `conftest.py` covering their consumers;
put ordinary factories, matchers, and workflow helpers in an explicitly imported
module. Follow the repository's test-support naming and BUILD conventions rather
than inventing a parallel `tests/` hierarchy.

Use actual Pydantic models, dataclasses, and other data containers for test data.
Mock services, clients, or I/O behavior when the test needs that boundary. A mock
of a data holder bypasses its required fields and validation and can hide schema
changes; sharing that mock only spreads the problem.

Keep an extraction local or decline it when:

- Test-specific roles, permissions, or inputs explain the assertion.
- A helper would obscure the dependencies or behavior being tested.
- Similar implementations belong to contracts expected to diverge, such as API versions.

For interaction helpers, retain readiness checks and await every operation. For
resource fixtures, preserve teardown on failure and each test's isolation. Retain
all meaningful cases and oracles, including snapshots and visual captures; only
merge cases when their coverage actually overlaps.

## Report and verification

For each finding, provide all source locations, the common contract, the proposed
owner/helper, the caller-specific differences to preserve, and the expected net
reduction. Explain why sharing is preferable to leaving the occurrences local.

When implementing, update every caller and build dependency in the same change,
then run the affected Bazel tests and checks. Confirm the refactor retains distinct
cases and assertions. If proposing a duplication CI check, explain how it would
distinguish actionable matches from intentional repetition and divergent contracts.

## References

- [Pytest fixtures](https://docs.pytest.org/en/stable/fixture.html)
- [PyHamcrest matchers](https://github.com/hamcrest/PyHamcrest)
- [Refactoring test code](https://martinfowler.com/articles/refactoring-test-code.html)
