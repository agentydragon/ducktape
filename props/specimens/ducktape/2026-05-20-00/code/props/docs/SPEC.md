# Props System Specification

Concise feature/requirement list for conformance checking across agents, backend, and frontend.

SPEC.md is append-only. TODO.md files in each component track implementation progress.

---

## Agent Prompt Principles

### Show Over Retell

Agent prompts should **reference readable sources** rather than duplicating information that the agent can read at runtime. Agents can read and understand Python source code. They don't have the entire props library and backend in their container, but they can inspect how the bundled modules they depend on are implemented.

**Prefer:**

> You can use the `run_critic` tool to ask the backend to run a critic agent. Read `props.agents.critic_dev.eval_client` to find its implementation details so you can do advanced operations (e.g., multiple critic runs in parallel).

**Avoid:**

> The `run_critic` tool accepts these arguments: `definition_id` (str), `example` (ExampleSpec, which is a discriminated union of WholeSnapshotExample with fields kind="whole_snapshot" and snapshot_slug, or SingleFileSetExample with fields kind="file_set", snapshot_slug, and files_hash), `timeout_seconds` (int), `budget_usd` (float). It returns a RunCriticResponse with critic_run_id (str). To run multiple critics in parallel, use asyncio.gather(...). [30 more lines of API details]

**Why:** The agent can `inspect.getsource()` the module and read the exact same information with full type signatures, docstrings, and implementation context. Retelling it in the prompt wastes tokens and creates a maintenance burden — the prompt drifts out of sync with the code.

**Apply this to:**

- Tool argument types and return types — point to the module defining them
- Database schema — use `describe_relation()` (reads live SQLAlchemy metadata) rather than hand-written schema docs
- Container layout and runfiles paths — let agents discover via `exec` + filesystem commands
- Workflow details like crane commands — show one example, then point to source for edge cases

**When retelling is justified:**

- High-level orientation that agents need before they can know _what_ to read
- Constraints not visible in source code (RLS policies, network topology, budget enforcement)
- Workflow sequences that span multiple modules
- Corrections to behavior that source code alone would mislead about

### Tool Schema via Pydantic Metadata

Tool behavior, field semantics, and argument constraints should be defined in Pydantic `Field(description=...)` on the tool argument models, not re-described in system prompts. The OpenAI API forwards these descriptions to the model automatically.

**Where to put tool documentation:**

- **Field semantics, types, constraints** → `Field(description=...)` on the Pydantic model
- **Tool purpose, return value** → tool function docstring (becomes the tool's `description`)
- **Workflow guidance** (when to use which tool, in what order) → system prompt
- **Constraints not in the schema** (RLS, budget enforcement, network topology) → system prompt

System prompts should describe _what the agent should accomplish_ and _workflow sequences_, not _what each tool argument means_.

### Exec Tool and Python Runtime

All agent containers must provide a working `python3` (or `python`) command at a simple path — not a deeply nested runfiles path. This Python must have the `props` library (and its dependencies) on the import path.

**Requirements:**

- `exec(["python3", "-c", "import props; ..."])` must work from any agent container
- `exec(["python3", "-c", "import inspect, props.agents.runtime; print(inspect.getsource(props.agents.runtime))"])` must print the module source
- Agents should be told to use `python3` for source inspection, not given container-specific paths

**Testing:** An E2E test must verify that the exec tool can import and inspect props source code. This validates that the "show over retell" principle actually works — if an agent prompt says "read `props.foo.bar` for details", the agent must be able to do so.

### Agent Autonomy

Agents have a set of convenience tools (exec, insert_issue, run_critic, etc.) but they are not limited to these tools. They can freely use `python3` to write scripts, query the database directly, call the backend API via HTTP, or build their own helper tools inside their container.

**What matters is the goal**, not the method:

- **Critics**: insert accurate critiques of the source code into the database
- **Graders**: create grading edges that correctly match critique issues against ground truth
- **Critic-dev agents**: produce new critic definitions with improved evaluated metrics

The provided tools are convenience shortcuts, not constraints. An agent can accomplish the same things by writing Python that calls the database or backend directly. Prompts should tell agents: "You can use the `run_critic` tool, or read `props.agents.critic_dev.eval_client` and call the backend API directly from Python — whatever works."

### What Agents Need to Know

Each agent type needs sufficient documentation and tooling in its prompt. The table below summarizes capabilities by agent type:

| Capability                                     | Critic     | Grader     | Critic-dev                |
| ---------------------------------------------- | ---------- | ---------- | ------------------------- |
| **exec** (shell commands, python3)             | Yes        | Yes        | Yes                       |
| **Fetch/read snapshot source**                 | Yes (auto) | Yes (auto) | Yes (fetch_snapshot tool) |
| **Read ground truth** (TRAIN split, RLS)       | No         | Yes        | Yes (SQL)                 |
| **Insert/submit issues**                       | Yes        | —          | —                         |
| **Grade issues against GT**                    | —          | Yes        | —                         |
| **Pull/edit/push definitions** (crane)         | —          | —          | Yes                       |
| **Run critics** (REST API)                     | —          | —          | Yes                       |
| **Wait for grading** (poll DB)                 | —          | —          | Yes                       |
| **Read grading data** (recall views)           | —          | —          | Yes                       |
| **Inspect bundled source** (inspect.getsource) | Yes        | Yes        | Yes                       |

Critic-dev agents perform the full definition development loop:

1. **Pull** existing definition images from the registry (`crane export`/`crane config`)
2. **Edit** definitions by overlaying files at the correct runfiles path
3. **Push** new definitions by digest (`crane mutate --append` + `crane push`)
4. **Run** critics via `run_critic` tool (blocks until exit)
5. **Wait** for grading via `wait_until_graded_tool` (polls until complete)
6. **Read** grading data from SQL views (`recall_by_definition_split_kind`, `recall_by_definition_example`, `tp_occurrence_credits`)
7. **Read** snapshot source code and ground truth to understand what critics should find

---

## Dashboard Views

### 1. Definitions Browser

**Purpose:** Manage and browse all agent definitions

- List all definitions with metadata (ID, type, created_at)
- Filter by agent type (critic, grader, etc.)
- Click through to see runs for a definition
- Separate page/table from leaderboard

### 2. Definitions Leaderboard

**Purpose:** Compare critic definition performance across splits/scopes

- Show ALL definitions (including those with no runs yet)
- 3-level header hierarchy:
  - Level 1: Split (Valid, Train)
  - Level 2: Example kind with count (whole_snapshot (n=X), file_set (n=Y))
  - Level 3: Metrics (Recall, Runs, Zero, Done, Stalled)
- Per-group columns:
  - Recall: Mean +/- margin (e.g., "45% +/- 3%")
  - Runs: evaluated/total count
  - Zero: Count with 0% recall
  - Done: Completed runs
  - Stalled: Max turns exceeded
- Sortable by any column
- Definition age shown
- Click recall cell -> opens run trigger modal prefilled with definition/split/kind

**Stats & Analysis section** (below leaderboard on overview page):

- Distribution charts (Chart.js bar charts):
  - Recall histogram (binned recall scores across runs)
  - TP count histogram (number of TPs found per run)
- Coverage heatmap: definition x example matrix showing evaluation coverage (uses chartjs-chart-matrix)
- Split toggle (valid/train) to filter all charts

### 3. Definition Detail

**Purpose:** View details and runs for a single definition

- Package ID (OCI image digest)
- Stats table: split x kind metrics (recall, runs, zero, done, stalled)
- Embedded runs browser filtered to this definition
- Back navigation to leaderboard

### 4. Active Runs

**Purpose:** Monitor currently executing agent runs

- List all runs with status IN_PROGRESS
- Show: run ID, definition, agent type (critic/grader), model, example info
- Show container status (running, exited, exit code) — agent internals are opaque
- Periodic polling refresh
- Click to open run detail view

### 5. Run Detail

**Purpose:** Inspect a single agent run

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

- Full agent_runs table with pagination
- Filters: status, agent_type, definition, split, date range
- Columns: ID, type, definition, model, status, created_at, example
- Click through to run detail

### 7. Agent Launch Modal

**Purpose:** Launch agent runs from the dashboard — validation runs, optimize agents, or improve agents.

Three-tab modal:

**Validation tab** (start critic evaluation runs on examples):

- Select critic definition (dropdown)
- Select split (train/valid)
- Select example kind (whole_snapshot/file_set)
- Set sample count (1-50)
- Critic model, budget per critic ($)

**Optimize tab** (launch a critic-dev optimizer agent):

- Target metric: whole-repo or targeted
- Optimizer model, critic model
- Budget ($), timeout (seconds)

**Improve tab** (launch a critic-dev improvement agent):

- Backend auto-selects best definition (by validation LCB) and top Pareto training examples
- Improvement model, critic model
- Number of examples, budget ($), timeout (seconds)

Triggered by:

- "Launch Agent" button in toolbar
- Clicking recall cell in definitions table (prefills validation tab)

On success, shows a link to the launched agent run.

### 8. Example Browser

**Purpose:** Browse and inspect training/validation examples

- List examples by snapshot, split, kind
- Show file paths for file_set examples
- Show TP/FP counts per example
- Click through to example detail with ground truth

---

## Snapshot and Critique Viewer

### 9. Snapshot Browser

**File Tree Navigation:**

- File manager-like view of snapshot content (from tar archives)
- Directory expansion/collapse
- File icons based on file type
- Click to navigate directories and open files
- Breadcrumb navigation for current path

**Issue Rollups:**

- On each file/directory entry, show counts of:
  - True Positive occurrences (TPs) in that file/subtree
  - False Positive occurrences (FPs) in that file/subtree
  - Count at disjoint issue/occurrence level (no double counting)
  - An occurrence counts toward a file if that file appears in the occurrence's files list
  - Occurrences spanning multiple files count toward each file separately
- Visual badges with counts (e.g., "3 TPs, 1 FP")
- Color coding: TPs in green, FPs in red

### 10. File Viewer with Issue Overlay

**Code Display:**

- Syntax highlighting based on file extension
- Line numbers (handle 0-based vs 1-based indexing correctly)
- Gutter for issue markers (GitHub-style)

**Issue Markers (Ground Truth):**

- Visual markers on affected line ranges
- An occurrence can span multiple files; each file can have multiple line ranges or none (whole file)
- Structure: `{ path: string, ranges: LineRange[] | null }`
- When `ranges === null`: highlight entire file or show file-level marker
- Distinct visual styles:
  - **True Positives**: Green left border, light green background
  - **False Positives**: Red left border, light red background
- Issue type icon in gutter
- Expandable comment-style view showing: issue ID, occurrence ID, rationale, note, all file locations, line ranges

**Issue Statistics (per occurrence):**

- Distribution of credits from critique runs: percent of runs where credit > 0, mean credit, histogram
- Displayed in issue detail panel

**Copyable URLs:**

- Button on each occurrence to copy URL like `/snapshots/{slug}/files/{path}#{tp_id}/{occurrence_id}`
- Deep-link directly to that occurrence when pasted
- For multi-file occurrences: URL points to primary file (first in files list)
- Occurrence detail panel shows links to all other affected files

### 11. Critique Viewer with Ground Truth Overlay

Shows critique run's reported issues overlaid on snapshot files:

- Each critique issue shows: issue ID, matched ground truth (if any) via grading_edges, credit received, grading rationale
- Visual distinction:
  - **Critique issues with TP match**: Blue left border
  - **Critique issues with FP match**: Orange left border
  - **Novel findings (no match)**: Gray left border
- Cross-referencing: click matched occurrence to jump to ground truth view
- Navigation: "Issue 7/15" counter, next/prev buttons, jump dropdown, filter by match status

### Visual Design

| Element                   | Color                               |
| ------------------------- | ----------------------------------- |
| TP occurrence             | Green (#dcfce7 bg, #16a34a border)  |
| FP occurrence             | Red (#fee2e2 bg, #dc2626 border)    |
| Critique issue (TP match) | Blue (#dbeafe bg, #2563eb border)   |
| Critique issue (FP match) | Orange (#fed7aa bg, #ea580c border) |
| Novel finding             | Gray (#f3f4f6 bg, #6b7280 border)   |

### Line Indexing

- Database stores 0-based line numbers
- Display shows 1-based line numbers
- File slicing uses 0-based

### Component Hierarchy

```
SnapshotDetailPage
├── SnapshotHeader (stats, metadata)
├── SnapshotBrowser
│   ├── FileTree
│   │   ├── DirectoryEntry (with issue counts)
│   │   └── FileEntry (with issue counts)
│   └── FileViewer
│       ├── CodeDisplay (syntax highlighted)
│       ├── LineGutter (line numbers + issue markers)
│       └── IssueOverlay
│           ├── OccurrenceMarker (TP/FP, with inline credit badge)
│           └── OccurrenceDetail (expandable)
│               ├── AllFileLocations
│               ├── OccurrenceStats
│               └── CopyUrlButton
├── DetectionStatsTab (aggregated occurrence stats table)
│   └── Per-occurrence: mean/min/max credit, run count
└── IssueNavigator (next/prev controls)

CritiqueDetailPage
├── CritiqueHeader
├── FileViewer (with critiqueIssues + gradingEdges props)
│   ├── CodeDisplay
│   ├── IssueMarker (TP/FP/Critique)
│   └── IssueDetail
│       ├── MatchedOccurrenceLink
│       └── CopyUrlButton
└── IssueNavigator
```

### Issue Rollup Statistics

- **At Issue Level**: total occurrences, per-occurrence % runs with credit > 0, mean credit, distribution, best/worst performing occurrence
- **At File Level**: total TPs/FPs, average detection rate
- **At Snapshot Level**: overall recall statistics, per-file breakdown

### URL Structure

**Snapshot views:**

- `/snapshots/{slug}` — snapshot browser (file tree + stats)
- `/snapshots/{slug}/files/{path}` — file viewer
- `/snapshots/{slug}/files/{path}#{tp_id}/{occ_id}` — deep link to occurrence

**Critique views:**

- `/runs/{run_id}/files/{path}` — critique overlay on file
- `/runs/{run_id}/files/{path}#{issue_id}` — deep link to critique issue

---

## API Endpoints

### Stats

- `GET /api/stats/overview` — leaderboard data with all definitions
- `GET /api/stats/definitions` — list definitions (filtered by agent_type)
- `GET /api/stats/occurrences` — aggregated per-occurrence credit stats (mean/min/max credit, run counts)
- `GET /api/stats/coverage` — definition x example coverage heatmap + recall/TP count histograms

### Runs

- `GET /api/runs` — browse all runs with filters/pagination
- `GET /api/runs/active` — currently executing runs
- `POST /api/runs/critic` — start a critic run (used by frontend UI and critic-dev agents)
- `POST /api/runs/optimize` — launch a critic-dev optimizer agent (target metric, budget, models, timeout)
- `POST /api/runs/improve` — launch a critic-dev improvement agent (auto-selects best definition and Pareto examples)
- `GET /api/runs/{id}` — single run detail
- `GET /api/runs/{id}/llm_requests` — LLM requests for a run (paginated)

### Examples

- `GET /api/examples` — list examples with filters (split, kind, snapshot)
- `GET /api/examples/{snapshot}/{kind}/{hash}` — example detail with ground truth

### Snapshot File Access

- `GET /api/gt/snapshots/{slug}/tree` — directory tree with issue counts
- `GET /api/gt/snapshots/{slug}/files/{path}` — file content
- `GET /api/gt/snapshots/{slug}/occurrences` — all occurrences with locations

### Occurrence Statistics

- `GET /api/gt/occurrences/{tp_id}/{occurrence_id}/stats` — credit distribution
- `GET /api/gt/snapshots/{slug}/stats` — snapshot-level statistics

### Critique Overlay

- `GET /api/runs/{run_id}/issues-with-locations` — critique issues with file locations
- Existing: `GET /api/runs/{run_id}` already has grading_edges

---

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

Terminal states determined by host scaffold from exit code:

- Exit 0 -> `EXITED`
- Timeout -> `TIMED_OUT`
- Non-zero exit -> `EXITED` (with non-zero `container_exit_code`)

---

## CLI Features Migrated to Frontend

The `props stats` CLI command and its subcommands (`critic-leaderboard`, `example`, `occurrence`) have been removed. All stats functionality is now served by the frontend dashboard via the Stats & Analysis section (overview page), Detection Stats tab (snapshot detail page), and the `/api/stats/*` endpoints.

---

## Future Extensions

### Critic Development Dashboard

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

### Implementation Phases (Snapshot/Critique Viewer)

1. **Backend API** — extract files from tar snapshots, serve content and directory tree
2. **Basic Snapshot Browser** — file tree, navigation, syntax highlighting, issue badges
3. **Issue Overlay** — occurrence markers on code, visual distinction, expandable details, copy URL
4. **Statistics Integration** — occurrence-level stats, credit distributions, aggregation
5. **Critique Viewer** — critique overlay, GT cross-referencing, navigation, match indicators
6. **Polish** — responsive design, loading states, error handling
