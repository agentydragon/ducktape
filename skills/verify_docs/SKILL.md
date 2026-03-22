---
name: verify-docs
description: Verify documentation claims against actual code, finding and fixing stale or incorrect docs. Audit docs for token efficiency — cut what strong LLMs already know, keep local specifics and gotchas. Use when asked to review docs, trim docs, or check docs are accurate.
---

# Verify and Optimize Documentation

Audit repo documentation for correctness and token efficiency.

## Principles

Docs are primarily consumed by LLM agents. Strong LLMs already know standard languages, frameworks, well-known APIs, and general SE practices. Only document:

- **Local specifics**: bespoke APIs, repo-specific patterns, non-obvious config
- **Gotchas**: things that previously broke, counterintuitive behavior
- **Deviations**: where this repo does something non-standard

Cut everything a strong LLM could derive from reading the code or from general knowledge.

## Standards

- **AGENTS.md**: open standard for AI coding agent instructions. Spec: <https://agents.md/>, repo: <https://github.com/agentsmd/agents.md>. Agents read the nearest AGENTS.md in the directory tree; monorepos use per-package files.
- **CLAUDE.md**: Claude Code's equivalent. Claude reads CLAUDE.md files hierarchically — all parents from repo root to current directory are loaded into context. Docs: <https://code.claude.com/docs/en/memory>
- **`@`-transclusion**: Claude Code feature where `@path/to/file` on its own line imports that file's content. Paths resolve relative to the importing file. Max depth: 5 hops.

## Audit procedure

1. **Discover conventions**: read root AGENTS.md, STYLE.md, README.md to learn this repo's doc structure, file hierarchy, and naming conventions. Don't assume — each repo may differ.
2. **Inventory**: find all documentation files (markdown, plaintext, in-code docs).
3. **Structural check**: verify the repo's own doc conventions are followed consistently.
4. **Staleness check**: for each claim in docs (file paths, function signatures, env vars, CLI flags, build targets), grep the codebase to verify it still exists and is accurate. Flag stale references.
5. **Token audit**: for each doc, flag:
   - General knowledge LLMs already have
   - Restated type signatures or parameter lists
   - Explanatory prose that doesn't add info beyond what the code shows
   - Redundant examples (one is enough per pattern)
   - Content duplicated between parent and child docs
   - Repo conventions restated in sub-docs that inherit from parent
6. **Classify kept content**: confirm remaining content falls into: local specifics, gotchas, deviations, bespoke API details, or recovery procedures for past failures.
7. **Propose changes**: present a concrete diff or list of cuts with rationale. Group by file.

## What to cut (examples)

- General framework knowledge ("pytest uses fixtures for shared setup")
- Standard library explanations (what `pathlib.Path` is)
- Obvious build/test commands (`bazel test //path:target`)
- Lengthy examples of standard patterns
- Style rules that restate language conventions (PEP 257, etc.)

## What to keep (examples)

- Non-obvious gotchas that caused real failures
- Bespoke API details not widely known
- Recovery procedures for past outages
- Repo-specific macros, entry points, or tooling
- Policy decisions that could go either way

## Prefer references over inline context

Reference external sources rather than restating them. Prescribe agents to fetch context on demand (WebFetch a URL, invoke a skill, read a file) instead of embedding the full content. This keeps docs small and avoids staleness.

## Structural improvements

- **Historical/stale docs should be clearly marked** — by convention, filename, banner, or directory placement (e.g., `archive/`). The mechanism doesn't matter; what matters is that agents can tell current docs from historical ones.
- **Verify link targets**: check all internal links actually resolve. Common issues: hyphens vs underscores, missing subdirectories, renamed files.
- **Cohesive flow over cumulative patches**: docs that grew by accretion should be restructured into logical sections. Group related content; don't just append. Choose an internal structure appropriate to the doc type — e.g., investigation notes: summary → timeline → diagnostics → root cause → fix. Decision docs: problem statement → requirements → options considered → decision. Don't force a single template; pick what fits.
- **Structural reshuffling**: when a doc is too large (>200 lines), consider splitting into focused sub-docs. When multiple small docs overlap, merge them. Rename/move files to better locations when the current placement is confusing. Extract reusable sub-docs that multiple parents can reference. The goal is docs that are cohesive, focused, and easy to navigate — not accumulated patches.
