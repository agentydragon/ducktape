---
name: knowledge_hygiene
description: Audit documentation, notes, runbooks, wiki-style knowledge bases, prompts, comments, UI/help copy, and other natural-language information systems for SSOT drift, duplication, stale claims, unclear ownership, and maintenance gaps. Produce prioritized, evidence-backed improvement suggestions with stable shorthand IDs like A, B, C so the user can choose what to execute.
---

# Knowledge Hygiene Audit

Improve natural-language knowledge systems: documentation, notes, runbooks, wikis, prompts, comments, help text, and other places where people or agents learn what is true.

## Scope

Use this skill when the user asks to improve docs, information architecture, knowledge bases, SSOT structure, duplication, stale guidance, or natural-language maintenance practices.

Audit artifacts such as:

- Markdown, plaintext docs, READMEs, runbooks, plans, TODOs, ADRs, and debug notes
- Wiki pages, support docs, internal handbooks, onboarding guides, and project notes
- Code comments, docstrings, CLI/help text, error messages, UI/admin copy, prompts, templates, examples, fixtures, and snapshots when they carry natural-language knowledge
- Schema descriptions, migration comments, config comments, generated documentation sources, and checked-in generated docs
- Issue/PR templates, release notes, changelogs, operational checklists, and automation output meant to be read as guidance

Code, config, tests, schemas, and runtime probes are evidence sources, not the primary target, unless the user explicitly expands scope.
Comments and docstrings are in scope when the problem is informational: e.g. they restate the name/signature, repeat obvious type facts, or describe a generic framework behavior without a local twist.

## Non-Goals

Do not produce code-quality recommendations from this skill. Exclude:

- Refactors, API redesigns, dead-code cleanup, lint enablement, formatting, or dependency upgrades
- Test coverage improvements, unless the proposed item is a docs/knowledge verification check
- Performance, security, reliability, or architecture changes whose primary fix is code
- General engineering best practices not tied to a concrete natural-language information problem

It is fine to use code, tests, generated artifacts, schemas, or live behavior to verify whether a natural-language claim is true.

If the user asks only for documentation correctness or token trimming in a repo, use `verify-docs`. If the task is a wrap-up after recent work, use `followups`.

## Process

### 1. Establish The Information System

Identify:

- Audience: developers, operators, end users, support, agents, future maintainers, or mixed
- Purpose: onboarding, operation, troubleshooting, design rationale, API reference, product guidance, project memory, or decision history
- Freshness expectation: current contract, historical record, draft plan, investigation notes, or archive
- Canonical homes: files, pages, sections, generated sources, schemas, owners, or systems of record
- Update path: who or what should change the information when the underlying truth changes

Do not assume every artifact should be current. Historical notes are fine when clearly marked and not masquerading as active guidance.

### 2. Build A Truth Map

Find durable claims and their apparent source of truth:

- Product or system behavior
- Setup, usage, operations, recovery, and deployment procedures
- API/data/configuration meanings
- Auth, permissions, privacy, or safety boundaries
- Naming, lifecycle, ownership, and deprecation state
- Known limitations, gotchas, and failure modes
- Decisions, rationale, rejected alternatives, and migration status

Flag claims that have no obvious canonical owner, appear in multiple competing places, or exist only in fragile locations such as comments, stale tickets, old plans, or copied examples.
Also flag volatile facts that merely mirror a canonical source, such as copied enum values, defaults, routes, flags, generated fields, service lists, table columns, dependency versions, or command output.

### 3. Verify Before Suggesting

Do not present guesses as findings. For each candidate improvement:

- Check exact files, pages, sections, anchors, names, commands, or examples still exist
- Compare claims against authoritative sources when available: schemas, code, tests, config, live docs, runtime behavior, generated outputs, or owner docs
- Search references before suggesting consolidation, renames, moves, deletion, or archival
- Confirm proposed verification steps are real and feasible
- Mark low-confidence items as `Investigation` instead of executable cleanup

For present-tense operational claims, prefer live evidence when available and appropriate.

### 4. Look For Knowledge Problems

Prioritize findings in these categories:

- **SSOT drift**: duplicated facts, conflicting explanations, unclear canonical owner, copied setup steps, generated output edited by hand
- **Documentation change-detectors**: copied volatile details that readers could look up from the source of truth, and that mainly create another place to update when the source changes
- **Zero-value obviousness**: comments, docstrings, README entries, examples, or help text that only restate names, signatures, headings, obvious file purposes, or generic tool behavior
- **Background-knowledge bloat**: explanations of standard language/framework/tool behavior that the intended audience or a strong agent can already infer, unless this repo intentionally differs
- **Staleness**: dead links, old commands, renamed concepts, outdated screenshots, obsolete support guidance, plans that look active after completion
- **Ambiguous status**: drafts, archives, investigations, and current contracts not distinguishable at a glance
- **Audience mismatch**: beginner tutorials mixed with operator runbooks, implementation notes inside user docs, historical rationale presented as current behavior
- **Navigation gaps**: important knowledge hard to find, missing index, weak cross-links, duplicate entry points for the same task
- **Token/noise cost**: generic explanations strong models already know, verbose restatements of schemas or signatures, repeated examples that do not add local knowledge
- **Actionability gaps**: troubleshooting steps without observable checks, runbooks without rollback or success criteria, examples without expected output
- **Maintenance gaps**: no owner, no regeneration path, no test/link check, no policy for tombstoning completed plans

Distinguish root problem from symptom. For example, three stale environment-variable descriptions may mean the real fix is a canonical config source and links to it, not editing all three copies.

Examples of information to remove or compress:

- Javadocs/docstrings that say `sortArray()` takes an array and returns it sorted, then repeat the return type already visible in the signature
- README sections that explain standard commands for an obvious artifact, such as running a plain Kubernetes manifest named `scrape-job.yaml` with `kubectl apply -f scrape-job.yaml`
- Boilerplate descriptions of fixtures, examples, or generated files where the filename and surrounding convention already convey the same fact
- Copied lists like "service `xyzzy` permits `foo` values `bar`, `baz`, `quux`" when `xyzzy.yaml` is the real owner of allowed values

If a standard mechanism differs here in one respect, keep only the deviation and point to the standard mechanism briefly. Example: "This is a Foo framework job; use normal `fooctl` job commands. Deviation: the job needs the `analytics-prod` profile."
Usually replace volatile mirrors with a pointer plus any durable meaning, policy, or gotcha the source does not contain. Generated Markdown/reference output is an exception, not the default; suggest it only when readers genuinely need the full volatile list inline and there is already a natural generation path.

### 5. Token-Cost Sweep

Token cost = size × load frequency. Inventory which surfaces are **always loaded**
into agent context versus read on demand:

- The instruction-file transclusion closure from repo root (e.g. `CLAUDE.md` →
  `AGENTS.md` → `README.md` + `@`-transcluded topic docs + style guides) — every session
- Skill `description:` frontmatter, MCP server instructions, generated session
  banners — every session
- Per-tree instruction files (transcluding their READMEs) — every session touching
  that tree
- Everything else — only when actually read

A 100-token cut in an always-loaded file outweighs a 1,000-token cut in an on-demand
doc. Sweep the always-loaded surfaces for, in descending value:

- **Teaching material for generic practice**: good/bad example pairs and rationale
  paragraphs for rules a strong model already follows. State the rule in one
  imperative line; keep an example only when it defines a repo-specific format.
- **Task-specific docs transcluded wholesale**: convert the transclusion to a one-line
  pointer so the doc loads on demand.
- **Duplication with skills or reference docs**: keep the two-line essentials inline
  and point at the canonical recipe.
- **Human-oriented depth shipped to agents via README transclusion**: deep reference,
  setup walkthroughs, and historical context move to linked (not transcluded) docs.
- **Repetition for emphasis**: a rule stated three times with escalating bold is one
  rule.

### 6. Rank Suggestions

Score each candidate by:

- Impact: user/operator/developer/agent confusion or risk reduced
- Confidence: directly verified evidence versus inference
- Cost: small edit, link/redirect, consolidation, archive/tombstone, source-of-truth pointer, ownership/process change
- Blast radius: one page, topic cluster, repo-wide docs, external KB, runtime docs
- Decay rate: likelihood of drifting further if ignored
- Load frequency: always-loaded agent context outweighs on-demand reading

Favor high-confidence, high-impact, low-cost fixes first.

## Output Format

Produce a concise report followed by an action menu. Every actionable item must have a stable shorthand identifier.

```markdown
## Findings

Reviewed 18 knowledge surfaces across onboarding, operations, API reference, and troubleshooting.
Found 2 high-priority SSOT fixes, 3 likely consolidations, and 2 optional archival cleanups.

## Action Menu

A. [P1] **Make setup instructions canonical in `docs/setup.md`**

- Evidence: `README.md`, `docs/onboarding.md`, and `runbooks/dev-env.md` give different install commands
- Why: New contributors can follow conflicting paths
- Change: Keep the full procedure in `docs/setup.md`; replace other copies with scoped summaries and links
- Verify: A reader can find exactly one full setup procedure, and other pages only summarize or link to it

B. [P2] **Tombstone completed migration plan**

- Evidence: `plans/auth-migration.md` reads active, but current docs and deploy config show the migration is complete
- Why: Agents may treat old migration work as pending
- Change: Add a completion banner, link to the current auth contract, and move remaining TODOs to the active tracker
- Verify: A reader can tell within the first screen that the plan is completed and where the current auth contract lives
```

### ID Rules

- Use `A`, `B`, `C`, then continue alphabetically.
- IDs must remain stable within the response and any immediate follow-up turn.
- If grouping by priority, keep one global ID sequence.
- If there are more than 26 items, use `AA`, `AB`, etc.
- Do not reuse an ID for a different item.

### Priority Markers

Put priority in the title immediately after the item ID:

- `[P0]` Critical
- `[P1]` High
- `[P2]` Medium
- `[P3]` Low
- `[P4]` Optional
- `[INV]` Investigation / not ready to execute

### Item Requirements

Each item includes:

- Shorthand ID, compact priority marker, and short title
- Evidence: exact files, pages, headings, links, quoted short snippets, commands, or observed behavior
- Why it matters
- Concrete proposed change
- Verification criterion: a command, search, link check, runtime check, or plain-language human review condition

Use short quotes only when necessary to identify the claim. Prefer paraphrase plus file/heading references.

## User Selection

After presenting the menu, ask which IDs the user wants executed.

Accept compact replies such as:

- `A`
- `A C F`
- `all high`
- `everything except D`
- `investigate B first`

When executing selected items:

- Confirm the selected IDs in one sentence
- Re-read the relevant artifacts before editing
- Keep changes scoped to the selected IDs
- Preserve unrelated worktree changes
- Run the verification listed for each completed item when feasible
- For human-language verification criteria, inspect the result and state whether the criterion is satisfied
- If a selected item becomes invalid after re-checking, stop that item and report why

Do not execute unselected menu items unless the user explicitly broadens scope.
