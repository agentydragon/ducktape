# Snapshot & Critique Viewer Specification

## Overview

A comprehensive UI for viewing ground truth snapshots and critique runs with GitHub-style review overlays, file browsing, and detailed statistics on grading performance.

## Core Features

### 1. Snapshot Browser View

**File Tree Navigation:**

- File manager-like view of snapshot content (from tar archives)
- Directory expansion/collapse
- File icons based on file type (use existing icon library like vscode-icons)
- Click to navigate directories and open files
- Breadcrumb navigation for current path

**Issue Rollups:**

- On each file/directory entry, show counts of:
  - True Positive occurrences (TPs) in that file/subtree
  - False Positive occurrences (FPs) in that file/subtree
  - Count at disjoint issue/occurrence level (no double counting)
  - **Note**: An occurrence counts toward a file if that file appears in the occurrence's files list
  - Occurrences spanning multiple files count toward each file separately
- Visual badges with counts (e.g., "3 TPs, 1 FP")
- Color coding: TPs in green, FPs in red

**Implementation Notes:**

- May not need to unpack/cache tar initially - can extract on-demand
- Need API endpoint: `GET /api/gt/snapshots/{slug}/files/{path}` to get file content
- Need API endpoint: `GET /api/gt/snapshots/{slug}/tree` to get directory structure with issue counts

### 2. File Viewer with Issue Overlay

**Code Display:**

- Syntax highlighting based on file extension (use highlight.js or prism.js)
- Line numbers (handle 0-based vs 1-based indexing correctly)
- Gutter for issue markers (GitHub-style)

**Issue Markers (Ground Truth):**

- Visual markers on affected line ranges
- **IMPORTANT**: An occurrence can:
  - Span multiple files (occurrence.files is a list)
  - Each file can have multiple line ranges OR no line ranges (whole file)
  - Structure: `{ path: string, ranges: LineRange[] | null }`
  - When `ranges === null`: Highlight entire file or show file-level marker
- Distinct visual styles:
  - **True Positives**: Green left border, light green background
  - **False Positives**: Red left border, light red background
- Issue type icon in gutter
- Expandable comment-style view showing:
  - Issue ID (tp_id/fp_id)
  - Occurrence ID
  - Rationale text
  - Note field (if present)
  - All file locations (may be multiple)
  - Line ranges (if specified, otherwise "whole file")

**Issue Statistics (per occurrence):**

- Show distribution of credits from critique runs:
  - Percent of runs where credit > 0
  - Mean credit value
  - Histogram/distribution of credit values
- Displayed in issue detail panel

**Copyable URLs:**

- Button on each occurrence to copy URL like:
  - `/snapshots/{slug}/files/{path}#{tp_id}/{occurrence_id}`
  - Should deep-link directly to that occurrence when pasted
  - For multi-file occurrences: URL points to primary file (first in files list)
  - Occurrence detail panel shows links to all other affected files

### 3. Critique Viewer with Ground Truth Overlay

**Similar to File Viewer but showing:**

- Critique run's reported issues overlaid on snapshot files
- Each critique issue shows:
  - Issue ID from critique
  - Matched ground truth (if any) - via grading_edges
  - Credit received
  - Grading rationale
  - Visual distinction from ground truth:
    - **Critique issues with TP match**: Blue left border
    - **Critique issues with FP match**: Orange left border
    - **Novel findings (no match)**: Gray left border

**Cross-referencing:**

- Click on matched occurrence to jump to ground truth view
- Show "Matched to TP-123/occ-1 (0.85 credit)" in critique issue

**Navigation Controls:**

- "Issue 7/15" counter
- Next/Previous issue buttons
- Jump to issue dropdown
- Filter: Show all / Only matched / Only unmatched

### 4. Issue Rollup Statistics

**At Issue Level (aggregating across occurrences):**

- Total occurrences
- Per occurrence:
  - % of runs with credit > 0
  - Mean credit
  - Distribution
- At issue level:
  - Mean across occurrences
  - Best/worst performing occurrence

**At File Level:**

- Total TPs/FPs in file
- Average detection rate

**At Snapshot Level:**

- Overall recall statistics
- Per-file breakdown

### 5. Visual Design Requirements

**Color Scheme:**

| Element                   | Color                               | Purpose                     |
| ------------------------- | ----------------------------------- | --------------------------- |
| TP occurrence             | Green (#dcfce7 bg, #16a34a border)  | Ground truth true positive  |
| FP occurrence             | Red (#fee2e2 bg, #dc2626 border)    | Ground truth false positive |
| Critique issue (TP match) | Blue (#dbeafe bg, #2563eb border)   | Critique matched TP         |
| Critique issue (FP match) | Orange (#fed7aa bg, #ea580c border) | Critique matched FP         |
| Novel finding             | Gray (#f3f4f6 bg, #6b7280 border)   | Critique with no match      |

**Icons:**

- TP: CheckCircle icon (green)
- FP: XCircle icon (red)
- Critique matched: Link icon
- Novel: Question icon

### 6. Line Indexing

**Critical requirement:** Handle 0-based vs 1-based indexing correctly

- Database stores 0-based line numbers
- Display shows 1-based line numbers
- File slicing uses 0-based
- Conversion utilities:

  ```typescript
  function dbLineToDisplay(line: number): number {
    return line + 1;
  }
  function displayLineToDb(line: number): number {
    return line - 1;
  }
  ```

### 7. Component Hierarchy

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
│           ├── OccurrenceMarker (TP/FP - shows markers for this file's ranges)
│           └── OccurrenceDetail (expandable)
│               ├── AllFileLocations (list of all files this occurrence spans)
│               ├── OccurrenceStats
│               └── CopyUrlButton
└── IssueNavigator (next/prev controls)

CritiqueDetailPage
├── CritiqueHeader
├── FileViewer (with critiqueIssues + gradingEdges props)
│   ├── CodeDisplay
│   ├── IssueMarker (TP/FP/Critique)
│   └── IssueDetail
│       ├── MatchedOccurrenceLink (for critiques with grading edges)
│       └── CopyUrlButton
└── IssueNavigator
```

### 8. API Endpoints Needed

**Snapshot File Access:**

- `GET /api/gt/snapshots/{slug}/tree` - Directory tree with issue counts
- `GET /api/gt/snapshots/{slug}/files/{path}` - File content (raw or highlighted)
- `GET /api/gt/snapshots/{slug}/occurrences` - All occurrences with locations

**Occurrence Statistics:**

- `GET /api/gt/occurrences/{tp_id}/{occurrence_id}/stats` - Credit distribution
- `GET /api/gt/snapshots/{slug}/stats` - Snapshot-level statistics

**Critique Overlay:**

- `GET /api/runs/{run_id}/issues-with-locations` - Critique issues with file locations
- Existing: `GET /api/runs/{run_id}` already has grading_edges

### 9. Libraries to Use

**Syntax Highlighting:**

- highlight.js (lightweight, 190+ languages)
- Alternative: shiki (VS Code's highlighter, more accurate but heavier)

**File Icons:**

- vscode-icons (map file extensions to icon names)
- Material Icons or Lucide Icons for UI icons

**Existing Components to Reuse:**

- `GradingEdges.svelte` - for showing grading edges
- `RunIdLink.svelte`, `DefinitionIdLink.svelte` - for cross-references
- Existing status/formatting utilities
- `/routes/snapshots/[...slug]/+page.svelte` - current snapshot detail view (TP/FP list)

### 10. Existing Infrastructure

**Backend:**

- `GET /api/gt/snapshots` - Lists all snapshots with TP/FP counts
- `GET /api/gt/snapshots/{slug}` - Returns detailed snapshot with all TPs/FPs
- Snapshot storage: `$ADGN_PROPS_SPECIMENS_ROOT/{slug}/code/` contains source files
- `props_core.db.models.SnapshotFile` - Database table tracking files in snapshots

**Frontend:**

- `/routes/snapshots/+page.svelte` - List view of all snapshots
- `/routes/snapshots/[...slug]/+page.svelte` - Detail view (currently shows TP/FP lists)
- File location formatting already implemented (path with line ranges)

**Data Models:**

- `TpInfo`, `FpInfo` - Issue info with occurrences
- `TpOccurrenceInfo`, `FpOccurrenceInfo` - Occurrence with file locations
  - `files: FileLocationInfo[]` - Array of file locations (can be multiple files)
- `FileLocationInfo` - File path with optional line ranges
  - `path: string` - File path
  - `ranges: LineRangeInfo[] | null` - Line ranges (null = whole file)
- `LineRangeInfo` - start_line/end_line
  - DB stores 0-based, display shows 1-based
  - `start_line: number`, `end_line: number` (both inclusive)

### 11. URL Structure

**Snapshot Views:**

- `/snapshots/{slug}` - Snapshot browser (file tree + stats)
- `/snapshots/{slug}/files/{path}` - File viewer
- `/snapshots/{slug}/files/{path}#{tp_id}/{occ_id}` - Deep link to occurrence
- `/snapshots/{slug}/files/{path}#{fp_id}/{occ_id}` - Deep link to FP

**Critique Views:**

- `/runs/{run_id}/files/{path}` - Critique overlay on file
- `/runs/{run_id}/files/{path}#{issue_id}` - Deep link to critique issue
- `/runs/{run_id}/issue/{issue_id}` - Focus on specific issue

### 12. Implementation Phases

**Phase 1: Backend API (ground truth file access)**

- Extract files from tar snapshots
- Serve file content and directory tree
- Include occurrence locations in tree response

**Phase 2: Basic Snapshot Browser**

- File tree component
- Directory navigation
- File display with syntax highlighting
- Issue count badges

**Phase 3: Issue Overlay**

- Render occurrence markers on code
- Visual distinction (colors, icons)
- Expandable issue details
- Copy URL buttons

**Phase 4: Statistics Integration**

- Compute occurrence-level statistics
- Display credit distributions
- Aggregate to file/snapshot level

**Phase 5: Critique Viewer**

- Critique issue overlay
- Ground truth cross-referencing
- Navigation controls (next/prev issue)
- Match status indicators

**Phase 6: Polish**

- Responsive design
- Keyboard shortcuts
- Loading states
- Error handling

### 13. Open Questions

1. **Tar extraction:** Cache extracted files or extract on-demand?
   - Recommendation: Start with on-demand, add caching if performance issues
2. **Large files:** Virtualization for files with many issues?
   - Recommendation: Start simple, add virtualization if needed
3. **Binary files:** How to display non-text files?
   - Recommendation: Show metadata only, "Binary file" message
4. **Diff view:** Show before/after for FPs?
   - Recommendation: Phase 2 feature

### 14. Success Criteria

- [ ] Can browse all files in a snapshot
- [ ] Can see all TPs and FPs overlaid on source code
- [ ] Can copy URL to specific occurrence and share it
- [ ] Can see statistics on how well critiques detected each occurrence
- [ ] Can view critique run's issues overlaid on snapshot
- [ ] Can navigate between issues in a critique
- [ ] Visual distinction between TP/FP/critique is clear
- [ ] Line numbers align correctly (no off-by-one errors)
- [ ] Syntax highlighting works for common languages
- [ ] Issue counts in file tree match actual occurrences
