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
Comments and docstrings are in scope when the problem is informational: e.g. they restate the name/signature, repeat obvious type facts, or describe a generic framework behavior without a local deviation.

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
- Repo-defined conventions: discover and honor the structure the gardened repo documents for itself before proposing anything. Read its own style/contributing/`AGENTS.md`/`README.md`/`STYLE.md` first to learn its doc-file roles and transclusion rules (e.g. a `README.md`/`AGENTS.md`/`CLAUDE.md`/`STYLE.md` split, `@`-includes), where each _kind_ of knowledge is filed (`plans/`, `debug/`, `archive/`, `docs/`, `lessons_learned/`, `TODO.md`, `SPEC.md`), and its naming/dating/tombstone formats. Every relocation, promotion, archival, or filing you propose must land knowledge in the home that repo defines — not a generic default. Match an existing sibling's location and naming. If the repo defines no convention for a case, say so and propose one consistent with its existing structure rather than importing an outside one.

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
- **Documentation change-detectors**: copied volatile details that readers could look up from the source of truth, and that mainly create another place to update when the source changes. Includes **self-referential counts** — prose that states the size of an adjacent list/table ("Twelve things have to line up", "the three steps below", "we support 7 providers"). The number duplicates what the reader can see, adds nothing, and forces a prose edit (twelve→eleven→…) every time a row is added or removed, risking silent drift. Drop the count and let the list speak; if a count is genuinely load-bearing, derive it, don't restate it. If the audited repo has a style guide, propose recording the rule there so it applies at write time, not just during audits.
- **Zero-value obviousness**: comments, docstrings, README entries, examples, or help text that only restate names, signatures, headings, obvious file purposes, or generic tool behavior
- **Background-knowledge bloat / framework re-explanation**: explanations of standard language/framework/tool behavior that the intended audience or a strong agent can already infer. The test for each explanatory sentence about a well-known tool: would it be true in any repo using that tool? If yes, cut it — naming the mechanism is enough. Keep only what is false elsewhere: deviations, version pins, local config, gotchas.
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
- Sections that teach a well-known framework's stock behavior (how Flux reconciles from git, what a Kubernetes Deployment is, how pytest fixtures resolve, what OAuth scopes are) — name the mechanism and state the local deviations/specifics; delete the tutorial

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

- **Self-transclusion duplication**: when a host file `@`-transcludes another (e.g.
  `AGENTS.md` whose first line is `@README.md`), the transcluded content is already
  inlined into the host — every reader of the host sees it. So the host must not
  restate, summarize, or even add a "see `<README.md>`" pointer back to its own
  transcluded file: that is pure duplication (or a pointer to content already present).
  Resolve each `@`-include before judging duplication, and collapse the host to its
  `@`-line plus only the net-new content that is _not_ in the transcluded file (for a
  sub-`AGENTS.md`: only agent-only prescriptions absent from its README and from every
  parent `AGENTS.md`). A sub-`AGENTS.md` that adds nothing new should be just its
  `@`-include line.
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

### 6. Structure And Promotion Sweep

Organizing rule: a fact's canonical home is the file someone must touch when the
fact changes (**update locality**). The full treatment of a concept (the hub)
lives at the lowest common ancestor of its consumers; every other mention is a
one-line scoped pointer stating only the local deviation. Do not propose manually
maintained backlink / "who links here" notes — if backlinks become needed, that
is a format/tooling change, not doc content to hand-maintain.

Gather actions for:

- **Wrong home**: a hub far from the artifacts it describes; a concept consumed
  by two siblings but documented inside one (hoist to the common ancestor); a
  durable rule trapped in the wrong document kind — operational truth inside a
  `debug/` investigation, a decision still phrased as a plan, cross-component
  behavior explained in one code comment.
- **Missing update path**: a hub with no answer to "what change in the world
  forces an edit here, and how does the editor notice?" Prefer proposing a
  change-time convention near the artifact ("update <hub> when changing <X>")
  over a one-off correction — change-time gardening beats scheduled sweeps.
- **Promotion candidates** — knowledge living somewhere worse than a doc:
  - the same question investigated or explained more than once (agent session
    logs, PR review threads, chat) — the strongest signal;
  - commit/PR messages that explain a system rather than a change;
  - `debug/` notes whose conclusions are durable rules — extract the rule to
    docs with a pointer back; the frozen narrative stays put;
  - code comments that outgrew one location;
  - procedures improvised twice (promote to a runbook).
- **Demotion candidates**: a doc nothing references that hasn't changed while
  its subject did — verify once, then archive (dated) or delete.

Promotion bar by destination: always-loaded instruction chains take only what
every session needs; on-demand docs take durable facts with ≥2 consumers in
space or time; knowledge scoped to one code location stays a comment.

### 7. Code Documentation Sweep

The same lenses applied to comments, docstrings, and schema descriptions —
the tightest-locality knowledge surface:

- **Deletion test at comment scope**: a comment or docstring that would be true
  above any call to this API restates the signature — delete (name/signature
  restatement, Args/Returns echoes, historical "used to", section banners).
  Per-file scan prompts (e.g. `prompts/scans/useless_comments_and_docs.md`) are
  the execution arm; this sweep picks which trees to point them at, reporting
  high-density clusters rather than individual nitpicks.
- **Boundary moves, both directions**: a comment explaining cross-component
  flow, system architecture, or an operational procedure has outgrown its file
  — promote to docs, leave one line. A doc paragraph mirroring a volatile
  implementation detail belongs the other way: demote it to a comment next to
  the code that changes with it.
- **Lifecycle**: `CLEANUP(added <date>)` tombstones — the date records when the
  marker was added, never a deadline; only a met condition makes one
  actionable. Flag conditions lacking a verifiable gate. `SYNC:` marker pairs
  must still exist on both ends and point at each other.
- **Always-loaded exception**: Pydantic `Field(description=...)`, MCP tool
  docstrings, and anything else that ships into JSON schemas or tool
  definitions is paid by every session using that server — weight it with the
  always-loaded surfaces from the token sweep, not like ordinary comments
  (paid only on file read). Tool descriptions state contract and local
  semantics, never generic API mechanics the model already knows. Do NOT flag
  duplication between tool/field descriptions and MCP server instructions as a
  defect: many MCP clients never surface server instructions to the model, so
  semantics the model must see ride on the tool surface itself.

### 8. Rank Suggestions

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
