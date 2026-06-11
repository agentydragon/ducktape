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

- Package ID with copyable CLI command (`props agent-pkg fetch <id> ./`)
- Stats table: split × kind metrics (recall, runs, zero, done, stalled)
- Embedded runs browser filtered to this definition
- Back navigation to leaderboard

### 4. Active Runs

**Purpose:** Monitor currently executing agent runs

**Features:**

- List all runs with status IN_PROGRESS
- Show: run ID, definition, agent type (critic/grader), model, example info
- Show last event (concise preview)
- Live update (WebSocket or fast polling)
- Click to open run detail view

### 5. Run Detail

**Purpose:** Inspect a single agent run

**Features:**

- Run metadata: ID, type, definition, model, status, created_at
- Parent run link (for graders)
- Child run links (critic → grader)
- Events timeline rendered like chat:
  - User messages, assistant text, tool calls, outputs, reasoning
  - Sequence numbers
  - Full content (expandable for long items)
- Live update while in_progress
- Completion summary when done
- Grading summary (for grader runs): TP matches, FP hits, recall score

### 6. Runs Browser

**Purpose:** Search and filter all historical runs

**Features:**

- Full agent_runs table with pagination
- Filters: status, agent_type, definition, split, date range
- Columns: ID, type, definition, model, status, created_at, example
- Click through to run detail

### 7. Validation Trigger (Modal)

**Purpose:** Start evaluation runs on examples

**Features:**

- Modal dialog triggered by:
  - "New Run" button in jobs list
  - Clicking recall cell in definitions table (prefilled)
- Select critic definition (dropdown)
- Select split (train/valid)
- Select example kind (whole_snapshot/file_set)
- Set sample count (1-50)
- Jobs list shows triggered jobs with progress (in-memory, not persisted)

### 8. Example Browser

**Purpose:** Browse and inspect training/validation examples

**Features:**

- List examples by snapshot, split, kind
- Show file paths for file_set examples
- Show TP/FP counts per example
- Click through to example detail with ground truth

### 9. Live Rollout View

**Purpose:** Monitor validation batches in progress

**Features:**

- Grid of validation jobs with progress bars
- Per-job: completed/failed/total counts
- Per-run timeline: critic -> grader pairs
- Real-time status updates

## API Endpoints

### Stats

- `GET /api/stats/overview` - Leaderboard data with all definitions
- `GET /api/stats/definitions` - List definitions (filtered by agent_type)

### Runs

- `GET /api/runs` - Browse all runs with filters/pagination
- `GET /api/runs/active` - Currently executing runs
- `GET /api/runs/jobs` - Validation job status
- `POST /api/runs/validation` - Trigger validation (with split param)
- `GET /api/runs/{id}` - Single run detail
- `GET /api/runs/{id}/events` - Run events (paginated)
- `WS /api/runs/{id}/stream` - Live event stream

### Examples

- `GET /api/examples` - List examples with filters (split, kind, snapshot)
- `GET /api/examples/{snapshot}/{kind}/{hash}` - Example detail with ground truth

## Non-functional Requirements

- Structured logging to file and stdout
- IN_PROGRESS runs not counted in completed stats
- "X runs in progress" indicator when applicable
- WebSocket for live updates (vs polling)
- Typed API client (OpenAPI generated)

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

## Live Agent State Display

Real-time visibility into running agents.

### Agent Run States

Display current state of each active run:

- **Waiting**: Queued, awaiting semaphore slot
- **Initializing**: Container starting, init script running
- **Sampling**: LLM API call in flight
- **Processing**: Agent loop between tool calls
- **Tool Call**: Executing MCP tool
- **Completed/Failed/Exceeded**: Terminal states

### Live Event Stream

For each active run, show:

- Last N events (scrollable)
- Current tool call in progress (if any)
- Token counts / cost accumulator
- Turn counter vs max turns
- Time elapsed

### Batch Progress

For validation jobs:

- Progress bar: completed/total
- Success/failure/in-progress counts
- Estimated time remaining
- Abort button

---

## Future Extensions

### Prompt Optimization Dashboard

- Launch prompt optimizer runs from UI
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
