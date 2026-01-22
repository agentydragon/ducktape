# Properties Knowledge Base

## Purpose

- Single, reusable source of truth for the properties my LLM agents must satisfy.
- Decoupled from any one agent or prompt; this is durable input data for systems that enforce and improve agent quality.
- Some overlap is fine — favor covering everything that should be covered over minimizing entries.

## Repository Layout

- `standards/` — property definition files (Markdown), supports nested categories:
  - `standards/python/` — Python-specific properties
  - `standards/markdown/` — Markdown-specific properties
  - `standards/domain-types-and-units/` — Domain-specific properties
  - `standards/` (root) — language-agnostic properties

## Conventions

- Property IDs are kebab-case and derived from filenames; evolve content rather than renaming IDs when possible.
- Overlap between properties is acceptable; a de-duplication layer can live above this knowledge base later.
- No indexes or generated cross-references for now.
- All Markdown in this repository (properties, specimens, docs) MUST adhere to the Markdown properties under `standards/markdown/**`. When writing/editing Markdown, follow those definitions as the normative style/structure.

## Property Files

- Location: under `standards/` (may be nested, e.g., `standards/python/<id>.md`, `standards/markdown/<id>.md`, or at the root for general)
- Identifier: read from the filename (no frontmatter ID)
- Required frontmatter:
  - `title` (required); do not duplicate the title in the body; keep it only in frontmatter.
  - `kind` (`behavior` | `outcome`); required
  - Do not include severity, status, owner, created date, tags, or related-properties lists.
- Body structure:
  - Predicate sentence (what holds true)
  - Acceptance criteria (checklist)
  - Positive examples (minimal good cases)
  - Negative examples (minimal anti-patterns)
  - Where other properties are mentioned/referenced inline, use standard links
    - e.g. `This example also violates [safe edits only](../properties/safe-edits-only.md).`
- Keep embedded code/diff snippets concise (≤ ~30 lines).

## GAP Markers

- Use the literal prefix `GAP:` to flag a missing or not‑yet‑defined rule/definition when documenting findings.
- Purpose: capture clarity/consistency gaps that do not have a precise property yet (e.g., confusing responsibility boundaries), even if an item is already covered by another property (like no‑dead‑code).
- Placement: put a standalone line starting with `GAP:` immediately after the finding bullet it annotates in covered.md or not_covered_yet.md. Keep to one or two sentences.
- Style: uppercase `GAP:` exactly; no parentheses/brackets; freeform explanatory text follows. Grep‑friendly and easy to scan.
- Lifecycle: when a property is added that covers the gap, remove the GAP note and link to the new property instead.
- Covered + GAP: It's acceptable to include a `GAP:` note under a covered finding when the item is covered at one level (e.g., "no-dead-code") but still lacks a clarity/abstraction‑level rule; use GAP to communicate partial coverage and the missing angle.

Example usage:

```markdown
- **wt/wt/server/gitstatusd_client.py**: 294–355 — [no-dead-code rationale]
  GAP: Clarify boundary vs helper responsibility for short‑array handling so index checks live in one place.
```

## Behavioral Layer and Scoping

- Evaluation/refactoring scope (for example, "only evaluate/refactor starting from edited hunks") is handled by agent behavioral instructions (critics/reviewers/fixers) and is orthogonal to property definitions.
- Properties should remain scope-agnostic; avoid embedding "agent-edited only" limits in property docs.
- Tooling supplies a freeform scope to agents:
  - If scope resolves to a diff range: the diff hunks define where to start reviewing/editing. Allow minimal cascades and necessary out-of-hunk edits to bring all touched code into compliance, then stop.
  - If scope resolves to static files: evaluate/edit the full files.

## Specimen-Driven Property Evolution (Freeform → Formal)

- Goal: Use real "I don't like this code" specimens to iteratively design properties and improve reviewer prompts.
- Process overview:
  1. Capture a specimen: code + a freeform list of review items (things that should be found, and optionally "negatives" that are OK and should not be flagged).
  2. Draft or refine a property definition from the specimen items (manually or via LLM-assisted prompt/design iteration).
  3. Generate/adjust reviewer prompts (critics/fixers/analyzers) from the property definition.
  4. Backtest: run analyzers on the specimen and measure:
     - Did it complain about what it should have complained about?
     - Did it avoid flagging the items explicitly marked as acceptable?
  5. Feedback loop:
     - If the reviewer finds novel, useful issues not in the specimen, add them as new "should find" items.
     - If the reviewer falsely flags acceptable patterns, add them as "negatives" (do-not-flag) to the specimen and/or clarify the property.
  6. Freeze specimens as ground truth snapshots; properties remain scope-agnostic and durable.
- This keeps properties concise and objective, while allowing rich freeform context during discovery and tuning.
