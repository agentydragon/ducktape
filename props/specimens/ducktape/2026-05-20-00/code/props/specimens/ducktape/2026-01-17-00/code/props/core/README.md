# LLM Properties Knowledge Base

## Purpose

- Single, reusable source of truth for the properties my LLM agents must satisfy.
- Decoupled from any one agent or prompt; this is durable input data for systems that enforce and improve agent quality.
- Some overlap is fine — favor covering everything that should be covered over minimizing entries.

## Repository layout (package)

- `props/` — property definition files (Markdown), supports nested categories:
  - `props/python/` — Python-specific properties
  - `props/markdown/` — Markdown-specific properties
  - `props/` (root) — language-agnostic properties
- `agent_defs/` — agent definition packages (critic, grader, prompt_optimizer, etc.)
- `db/` — database models and migrations
- `TODO.md` — open questions and planned extensions

## Specimens Dataset

**Specimen data now lives in a separate repository**: [github.com/agentydragon/specimens](https://github.com/agentydragon/specimens)

Specimens are frozen code states with labeled issues (true positives and false positives) used for training and evaluating the LLM critic. The dataset includes:

- Per-snapshot directories with `manifest.yaml` (source, split, bundle metadata) and issue files (`.yaml`)
- Each snapshot has its own `manifest.yaml` defining source commit and train/valid/test split

### Configuration

Set the `ADGN_PROPS_SPECIMENS_ROOT` environment variable to point to the specimens repository:

```bash
export ADGN_PROPS_SPECIMENS_ROOT=/path/to/specimens
```

When using direnv (recommended), this is configured in `.envrc`:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
export ADGN_PROPS_SPECIMENS_ROOT="$REPO_ROOT/../specimens"
```

**Required**: The environment variable must be set. The package will raise an error if it's not configured.

### Authoring

See the [specimens repository](https://github.com/agentydragon/specimens) for format specs and authoring guides.

## Conventions

- Property IDs are kebab-case and derived from filenames; evolve content rather than renaming IDs when possible.
- Overlap between properties is acceptable; a de-duplication layer can live above this knowledge base later.
- No indexes or generated cross-references for now.
- All Markdown in this repository (properties, specimens, docs) MUST adhere to the Markdown properties under `props/markdown/**`. When writing/editing Markdown, follow those definitions as the normative style/structure.

## Property files

- Location: under `props/` (may be nested, e.g., `props/python/<id>.md`, `props/markdown/<id>.md`, or at the root for general)
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

## GAP markers

- Use the literal prefix `GAP:` to flag a missing or not‑yet‑defined rule/definition when documenting findings.
- Purpose: capture clarity/consistency gaps that do not have a precise property yet (e.g., confusing responsibility boundaries), even if an item is already covered by another property (like no‑dead‑code).
- Placement: put a standalone line starting with `GAP:` immediately after the finding bullet it annotates in covered.md or not_covered_yet.md. Keep to one or two sentences.
- Style: uppercase `GAP:` exactly; no parentheses/brackets; freeform explanatory text follows. Grep‑friendly and easy to scan.
- Lifecycle: when a property is added that covers the gap, remove the GAP note and link to the new property instead.
- Covered + GAP: It’s acceptable to include a `GAP:` note under a covered finding when the item is covered at one level (e.g., “no-dead-code”) but still lacks a clarity/abstraction‑level rule; use GAP to communicate partial coverage and the missing angle.

Example usage:

```markdown
- **wt/wt/server/gitstatusd_client.py**: 294–355 — [no-dead-code rationale]
  GAP: Clarify boundary vs helper responsibility for short‑array handling so index checks live in one place.
```

## Behavioral layer and scoping

- Evaluation/refactoring scope (for example, “only evaluate/refactor starting from edited hunks”) is handled by agent behavioral instructions (critics/reviewers/fixers) and is orthogonal to property definitions.
- Properties should remain scope-agnostic; avoid embedding "agent-edited only" limits in property docs.
- Tooling supplies a freeform scope to agents:
  - If scope resolves to a diff range: the diff hunks define where to start reviewing/editing. Allow minimal cascades and necessary out-of-hunk edits to bring all touched code into compliance, then stop.
  - If scope resolves to static files: evaluate/edit the full files.

## Specimen-driven property evolution (freeform → formal)

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

## Training Strategy: Per-File Examples

**Goal:** Train the LLM critic to behavior-clone the user's subjective code review judgment by using fine-grained training examples.

**Approach:** Generate multiple focused training examples per snapshot (single files, file pairs, component groups) in addition to the full-repo review. This provides tighter feedback loops and more training signal for optimization.

**Dataset model:**

- **Snapshot:** Frozen code state at a specific commit with labeled issues (TPs and FPs) — specimens from separate repo
- **Training Example:** `(snapshot, targeted_files)` pair where recall denominator is computed based on which issues are in expected recall scope for those files
- **True Positive filtering:** Uses `critic_scopes_expected_to_recall` to determine which issues should be detectable given a file set

For detailed information, see [Training Strategy](docs/training_strategy.md).

```mermaid
flowchart TD
  A[Specimen: code + freeform review items] --> B[Draft/refine property definition]
  B --> C[Generate/adjust reviewer prompts]
  C --> D[Run analyzers/reviewers on specimen]
  D --> E{Backtest results}
  E -->|Found expected issues| F[Success metrics ↑]
  E -->|Missed expected issues| B
  E -->|Flagged acceptable items| C
  D --> G{Novel findings?}
  G -->|Yes| H[Augment specimen: add "should find" / "do-not-flag"]
  H --> D
  G -->|No| I[Freeze specimen snapshot]

  %% Also allow direct property → reviewers check on arbitrary code
  B -.-> J[LLM analyzers check arbitrary code]
  J -.-> E
```

## Specimen inspection (for assistants)

**Note:** The `snapshot exec` command is currently disabled. Snapshot source code is now stored in PostgreSQL and fetched by agent init scripts at runtime. To inspect specimen files, query the database directly or use the sync'd specimens repository.

## Usage Workflow

### 1. Run Critic on a Specimen

Run structured critic to find issues in a specimen:

```bash
# Run critic with default preset (max-recall-critic)
props run --snapshot ducktape/2025-11-20-00 --structured true

# Or specify a custom preset
props run --snapshot ducktape/2025-11-20-00 --structured true --preset find

# Filter to specific files
props run --snapshot ducktape/2025-11-20-00 --structured true --files src/foo.py src/bar.py
```

This:

- Loads the specimen from the registry
- Runs the critic agent with MCP tools (Docker-based)
- Stores the critique in the database
- Returns the critique_id for grading

### 2. Grade a Critique

Grade a stored critique against canonical findings:

```bash
# Grade by critique ID (from previous critic run output)
props snapshot-grade 123

# Use different model for grading
props snapshot-grade 123 --model gpt-4o
```

This:

- Fetches the critique from the database
- Loads the specimen's canonical issues
- Runs the grader to compute metrics (TP/FP/FN/recall/precision)
- Stores grader results in the database

### 3. Query Results

Query stored agent runs from the database:

```python
from props_core.db import get_session
from props_core.db.models import AgentRun, GradingDecision

with get_session() as session:
    # Get all agent runs for a snapshot
    runs = session.query(AgentRun).filter(
        AgentRun.type_config["snapshot_slug"].astext == "ducktape/2025-11-20-00"
    ).all()

    # Get grading decisions with metrics
    for d in session.query(GradingDecision).filter_by(agent_run_id=run_id):
        print(f"TP {d.tp_id}: credit={d.found_credit}")
```

All structured runs are persisted with:

- Input/output payloads (JSONB columns in database)
- Specimen splits for train/valid/test separation
- Execution traces in events table
