---
name: trimcode
description: Audit a repository for opportunities to reduce maintained code while meeting its important goals and requirements. Use when asked to cut lines of code, simplify a codebase, or find high-payoff code removal opportunities.
---

# Reduce repository code

Find ways to express the repository's intent with less code. Treat lines of code
(LOC) as a way to size opportunities, not a goal that overrides correctness,
clarity, operability, or required behavior. By default, report approximately ten
concrete opportunities, ordered by estimated net LOC saved. Implement changes
when the user asks for implementation; an audit request alone calls for findings.

Exact behavioral equivalence is not required for a recommendation. A simpler
design may intentionally change or drop incidental behavior when the saving is
worth the tradeoff in this project. Preserve the user-level flows and other
requirements that matter; use evidence about callers, contracts, and operational
needs to distinguish those from incidental details. Make proposed changes and
their consequences explicit rather than presenting them as equivalent.

## Understand and search

Before ranking candidates, read the repository's `TODO.md` or equivalent if present. For each
candidate, search by feature and path in the owning component's TODOs, README, plans, and decision
records. Honor decisions to keep, defer, or reject a change. Report any pending design choice,
trial, or migration as a gate to implementation. Revisit settled findings when the recorded
condition is met, relevant evidence changes, or the user reopens them.

Read the repository's instructions, goals, public interfaces, and relevant tests
and operational constraints. Map the main components and their dependencies;
inspect representative code across the requested scope, including large or
complex areas, instead of restricting the search to duplicates. State where the
survey was shallow or excluded code. Identify the requirements that explain why
apparently verbose code exists before proposing its removal.

Apply Chesterton's fence: when complexity's value is unclear, investigate why it
was introduced before recommending its removal. Trace relevant history and
context (such as `git log` and blame, documentation, debug notes, issues, and
linked changes where available) for hard-won fixes, failed approaches, and
non-obvious constraints. Check whether those reasons still apply and whether the
proposed simplification handles them. Cite the evidence; if the rationale remains
unknown, say so and identify what needs investigation or validation rather than
assuming the complexity is unnecessary.

Look at different scales, following evidence rather than a fixed quota:

- **Architecture and dependencies:** collapse needless layers, adapters and
  abstractions; replace bespoke subsystems with a suitable existing library,
  framework, standard database, or repository facility; reconsider the design
  when a simpler one can meet the same goals.
- **Generation and declarative sources:** replace hand-maintained repetitive code
  with an appropriate generator, schema, or existing build facility. Account for
  the source definitions, generator integration, generated artifacts, and their
  ongoing maintenance.
- **Local code:** consolidate repeated setup with shared fixtures or helpers,
  remove unused paths after tracing callers, and use language or library features
  available under the project's supported versions. Check that a helper actually
  improves clarity and doesn't hide meaningful differences.
- **Optional detail:** consider simplifying custom exception handling, logging,
  error messages, or test setup when the extra behavior is not worth its cost.
  For example, replacing custom handling that returns HTTP 400 with a framework's
  404 may be reasonable if callers, the API contract, and the failure semantics
  permit it; it is not automatically equivalent or safe in every context.
- **Tests:** find duplicate scenarios and tests that only verify well-established
  dependency or standard-library behavior. Compare inputs, assertions, execution
  paths, and failure modes with the proposed surviving tests. Assess whether
  repository-specific configuration, integration, edge cases, regressions, and
  contracts across dependency or version boundaries still need coverage. A shared
  line of code or a similar test name alone does not establish redundancy; if a
  test's behavior is intentionally dropped, explain the tradeoff instead.

Check proposed libraries, generators, and language features against authoritative
current documentation and the project's version, platform, licensing, maintenance,
security, and deployment constraints. For a replacement, trace the required
semantics and operational needs, including persistence and migration where
relevant. Flag gaps and risky assumptions rather than asserting equivalence.

## Size and report

For each finding, cite specific files and code or tests, explain the proposed
change and why it still meets the important goals and requirements, and give:

- Estimated LOC removed, added, and **net saved** (removed minus added), with a
  range and the scope and counting basis; identify measured versus inferred
  quantities. Count required glue, migration, configuration, tests, and new
  maintained definitions. Report generated LOC separately from hand-maintained
  LOC so generated output cannot inflate the practical saving.
- Evidence for feasibility, the behavior preserved and intentionally changed or
  removed, affected callers, key tradeoffs and missing evidence, and the
  validation needed before accepting the change. For test removals, name the
  surviving coverage, explain why only dependency behavior is tested, or describe
  the intentionally dropped behavior; identify any unique protection that must
  be retained.
- Effort and risk, including dependency and operational costs for replacements.

Order by estimated net maintained LOC saved, largest first, while making uncertain
estimates explicit. Don't add overlapping savings together: identify alternatives
and shared code, and state any combined estimate only for compatible changes.
Report fewer than ten when evidence does not support more; don't invent findings
or pretend the survey is exhaustive. Distinguish promising architectural
hypotheses requiring a prototype from directly verified local edits.
