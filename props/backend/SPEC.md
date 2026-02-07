# Props Dashboard Specification

Concise feature list for frontend + backend conformance checking.

SPEC.md is append-only. TODO.md tracks implementation progress.

## Views

### 1. Definitions Browser

**Purpose:** Manage and browse all agent definitions

**Features:**

- List all definitions with metadata (ID, type, created_at)
- Filter by agent type (critic, grader, etc.)
- Click through to see runs for a definition
- Separate page/table from leaderboard

### 2. Definitions Leaderboard

**Purpose:** Compare critic definition performance across splits/scopes

**Features:**

- Show ALL definitions (including those with no runs yet)
- 3-level header hierarchy:
  - Level 1: Split (Valid, Train)
  - Level 2: Example kind with count (whole_snapshot (n=X), file_set (n=Y))
  - Level 3: Metrics (Recall, Runs, Zero, Done, Stalled)
- Per-group columns:
  - Recall: Mean ± margin (e.g., "45% ± 3%")
  - Runs: evaluated/total count
  - Zero: Count with 0% recall
  - Done: Completed runs
  - Stalled: Max turns exceeded
- Sortable by any column
- Definition age shown
- Click recall cell → opens run trigger modal prefilled with definition/split/kind

### 3. Definition Detail

**Purpose:** View details and runs for a single definition

**Features:**

- Package ID (OCI image digest)
- Stats table: split × kind metrics (recall, runs, zero, done, stalled)
- Embedded runs browser filtered to this definition
- Back navigation to leaderboard

### 4. Active Runs

**Purpose:** Monitor currently executing agent runs

**Features:**

- List all runs with status IN_PROGRESS
- Show: run ID, definition, agent type (critic/grader), model, example info
- Show container status (running, exited, exit code) — agent internals are opaque
- Periodic polling refresh
- Click to open run detail view

### 5. Run Detail

**Purpose:** Inspect a single agent run

**Features:**

- Run metadata: ID, type, definition, model, status, created_at
- Parent run link (for child agents)
- Child run links
- LLM requests table (from `llm_requests`):
  - Model, latency, token counts, cost
  - Expandable request/response bodies
- Container stdout/stderr (from `agent_runs.container_stdout/stderr`)
- Completion summary when done
- Grading summary (for grader runs): TP matches, FP hits, recall score

### 6. Runs Browser

**Purpose:** Search and filter all historical runs

**Features:**

- Full agent_runs table with pagination
- Filters: status, agent_type, definition, split, date range
- Columns: ID, type, definition, model, status, created_at, example
- Click through to run detail

### 7. Critic Run Trigger (Modal)

**Purpose:** Start critic evaluation runs on examples (human user or critic-dev agent)

**Features:**

- Modal dialog triggered by:
  - "New Run" button in jobs list
  - Clicking recall cell in definitions table (prefilled)
- Select critic definition (dropdown)
- Select split (train/valid)
- Select example kind (whole_snapshot/file_set)
- Set sample count (1-50)
- Configure timeout_seconds and budget_usd (required)
- Jobs list shows triggered jobs with progress (in-memory, not persisted)

### 8. Example Browser

**Purpose:** Browse and inspect training/validation examples

**Features:**

- List examples by snapshot, split, kind
- Show file paths for file_set examples
- Show TP/FP counts per example
- Click through to example detail with ground truth

## API Endpoints

### Stats

- `GET /api/stats/overview` - Leaderboard data with all definitions
- `GET /api/stats/definitions` - List definitions (filtered by agent_type)

### Runs

- `GET /api/runs` - Browse all runs with filters/pagination
- `GET /api/runs/active` - Currently executing runs
- `POST /api/runs/critic` - Start a critic run (used by frontend UI and critic-dev agents)
- `GET /api/runs/{id}` - Single run detail
- `GET /api/runs/{id}/llm_requests` - LLM requests for a run (paginated)

### Examples

- `GET /api/examples` - List examples with filters (split, kind, snapshot)
- `GET /api/examples/{snapshot}/{kind}/{hash}` - Example detail with ground truth

## Non-functional Requirements

- Structured logging to file and stdout
- IN_PROGRESS runs not counted in completed stats
- "X runs in progress" indicator when applicable
- Typed API client (OpenAPI generated)

## Agent Status Model

Agent status is opaque from the dashboard's perspective:

- We see the container and its exit code
- LLM requests are logged by the proxy to the `llm_requests` table
- Container stdout/stderr captured after exit
- No real-time introspection into agent internals
- Agents query the LLM proxy as they see fit (or may never do so)

Terminal states are determined by the host scaffold from exit code:

- Exit 0 → `EXITED`
- Timeout → `TIMED_OUT`
- Non-zero exit → `EXITED` (with non-zero `container_exit_code`)

---

## CLI Features to Migrate

Features from `props` CLI to replicate in dashboard.

### `props stats` (main view)

Default view showing definition recall across splits/scopes:

- 4 column groups: Valid Whole, Valid Partial, Train Whole, Train Partial
- Per-group: Recall, LCB, N, Zero count, Max turns, Context exceeded
- Green highlighting for fully evaluated rows
- Sorted by valid whole recall descending

### `props stats critic-leaderboard`

Same as default stats but with additional filter options:

- Filter by split, example_kind
- Filter by definition name pattern
- Sort by different columns

### `props stats example`

Per-example metrics (not per-definition):

- Grouped by (snapshot, example_kind, files_hash)
- Shows: recall, n_runs, status breakdown
- Identify hard examples (consistently low recall)

### `props stats occurrence`

Per-occurrence statistics:

- Individual TP occurrences with hit rates across runs
- Find consistently-missed occurrences
- Useful for debugging specific issue patterns

---

## Future Extensions

### Critic Development Dashboard

- Launch critic-dev optimizer runs from UI
- Budget tracking and cost display
- Iteration history with metric trends
- Compare definitions side-by-side

### Ground Truth Management

- View TPs/FPs per snapshot
- Edit ground truth annotations
- Import/export ground truth

### Snapshot Management

- List snapshots with metadata
- Fetch new snapshots from git
- View file tree for snapshot

### Ground Truth Update Workflow

When ground truth changes (new TPs/FPs added/modified):

- Detect affected grader runs (referenced outdated ground truth)
- Option to invalidate/regrade affected runs
- Show which runs need regrading
- Batch regrade capability

**Existing infrastructure:**

- `GraderTypeConfig.canonical_issues_snapshot` stores TPs/FPs used at grading time
- `grader/staleness.py:identify_stale_runs()` compares stored snapshot to current ground truth
- `props stats` CLI already includes staleness check section

**Desired staleness detection:**

- Compare semantic content only: TP/FP IDs, rationales, occurrence locations (files + line ranges)
- Exclude `critic_scopes_expected_to_recall` (test coverage metadata, not grading content)

**Optimization approaches:**

1. **Timestamp-based:** Compare `updated_at` on ground truth vs `canonical_issues_snapshot_time` on grader run
2. **Sync-time marking:** `props sync` immediately marks affected runs as stale when updating ground truth
3. **Incremental regrading:** Instead of full regrade, append system message to existing run:
   "Ground truth updated for: TP-123, FP-456. Update affected grading decisions and resubmit."
   - Preserves existing work, patches only the delta
   - Requires tracking which TPs/FPs each grading decision references
