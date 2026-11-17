---
name: trimcode
description: Audit a repository for opportunities to reduce maintained code while preserving its goals and behavior. Use when asked to cut lines of code, simplify a codebase, or find high-payoff code removal opportunities.
---

# Reduce repository code

Find ways to express the repository's intent with less code. Treat lines of code
(LOC) as a way to size opportunities, not a goal that overrides correctness,
clarity, operability, or required behavior. By default, report approximately ten
concrete opportunities, ordered by estimated net LOC saved. Implement changes
when the user asks for implementation; an audit request alone calls for findings.

## Understand and search

Read the repository's instructions, goals, public interfaces, and relevant tests
and operational constraints. Map the main components and their dependencies;
inspect representative code across the requested scope, including large or
complex areas, instead of restricting the search to duplicates. State where the
survey was shallow or excluded code. Identify the requirements that explain why
apparently verbose code exists before proposing its removal.

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
- **Tests:** find duplicate scenarios and tests that only verify well-established
  dependency or standard-library behavior. Compare inputs, assertions, execution
  paths, and failure modes with the proposed surviving tests. Keep coverage of
  repository-specific configuration, integration, edge cases, regressions, and
  contracts across dependency or version boundaries. A shared line of code or a
  similar test name alone does not establish redundant coverage.

Check proposed libraries, generators, and language features against authoritative
current documentation and the project's version, platform, licensing, maintenance,
security, and deployment constraints. For a replacement, trace the required
semantics and operational needs, including persistence and migration where
relevant. Flag gaps and risky assumptions rather than asserting equivalence.

## Size and report

For each finding, cite specific files and code or tests, explain the proposed
change and why it preserves the intended behavior, and give:

- Estimated LOC removed, added, and **net saved** (removed minus added), with a
  range and the scope and counting basis; identify measured versus inferred
  quantities. Count required glue, migration, configuration, tests, and new
  maintained definitions. Report generated LOC separately from hand-maintained
  LOC so generated output cannot inflate the practical saving.
- Evidence for feasibility and preserved behavior, key tradeoffs and missing
  evidence, and the validation needed before accepting the change. For test
  removals, name the surviving coverage or explain why only dependency behavior
  is tested; identify any unique protection that must be retained.
- Effort and risk, including dependency and operational costs for replacements.

Order by estimated net maintained LOC saved, largest first, while making uncertain
estimates explicit. Don't add overlapping savings together: identify alternatives
and shared code, and state any combined estimate only for compatible changes.
Report fewer than ten when evidence does not support more; don't invent findings
or pretend the survey is exhaustive. Distinguish promising architectural
hypotheses requiring a prototype from directly verified local edits.
