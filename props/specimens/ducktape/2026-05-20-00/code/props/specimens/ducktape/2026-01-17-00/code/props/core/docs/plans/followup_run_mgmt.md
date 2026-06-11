# Props Run Management: Implementation Plan

## Remaining Work 🎯

### High Priority

1. **Convert lint_issue to proper run structure**
   - Currently: Writes to `logs/mini_codex/lint_issue/run_{ts}_{pid}/events.jsonl`
   - Should: Follow 3-file pattern in runs/ structure
   - Target: `runs/lint_issue/specimen:{slug}/{issue_id}/{timestamp}/` with input.json (IssueCore+Occurrence), output.json (LintSubmitPayload), events.jsonl
   - Structure: issue_id as path component under specimen scope
   - Note: snapshot exec is just a Docker shell command (no agent, no outputs needed)

### Eval Harness

2. **Implement evaluation samples**
   - Current: No evaluation cases defined
   - Should: Populate with actual IssueEvalSpec instances from git-tracked spec files
   - Specs/test cases stay in git (not database)
   - Needs:
     - Select representative specimens and issues for evaluation
     - Define expectations per occurrence (anchor windows, rationale rubrics, findings matchers)
     - Create IssueEvalSpec instances with OccurrenceCase objects
   - Example structure: For specimen "ducktape/2025-11-20-adgn" + issue "dead-code", define 3-5 cases with different code patterns and expected ranges
   - Benefits: Enables automated quality checks on linter output

### Medium Priority

3. **Test coverage improvements**
   - Tests for lint_issue structure conformance
   - Tests for eval harness when samples are added

## Path Construction Audit

All places that construct paths in the props codebase, classified by action needed:

### 🔧 NEEDS FIXING - Path Antipatterns

1. **lint_issue.py lines 462-463**
   - `Path.cwd() / "logs" / "mini_codex" / "lint_issue"`
   - Action: 🔧 Convert to proper run structure (Task #1)
   - Target: Use LintIssueRun manager → `runs/lint_issue/specimen:{slug}/{issue_id}/{timestamp}/`

2. **agent_runner.py lines 48-49**
   - `Path.cwd() / "logs" / "mini_codex" / "agent_runner"`
   - Action: ✅ Keep as-is (utility function for ad-hoc prompt testing, not formal runs)

### ✅ CORRECT - Non-Runs Path Construction

3. **cluster_unknowns.py line 190**
   - `out_root / "clusters.json"`
   - Action: ✅ Keep as-is (cluster output structure is defined by RunsContext)

4. **specimens/registry.py** (multiple lines)
   - Specimen manifest and source path resolution
   - Action: ✅ Keep as-is (specimen registry, not runs management)

5. **~~bundles/build_bundle.py~~** (DELETED in bundle→plain files migration)
   - Bundle creation paths
   - Action: ~~Keep as-is~~ → Removed (migrated to plain files workflow)

6. **cli_app/main.py lines 229, 670**
   - `Path(tempfile.gettempdir()) / "adgn_codex_prompts"`
   - Action: ✅ Keep as-is (temporary prompt storage for debugging)

7. **cli_app/main.py lines 294, 354, 360, 375, 379, 534, 551**
   - Various output files within run directories (results.json, prompt.txt, etc.)
   - Action: ✅ Keep as-is (writing artifacts within RunsContext-derived directories)

## Directory Structure

```
runs/
  cluster/                    # Cluster unknowns workflow
    YYYYMMDD_HHMMSS/
      {project}/{date}/       # Per-specimen cluster outputs
        clusters.json

  prompt_optimize/            # Prompt optimization sessions (nested by timestamp)
    YYYYMMDD_HHMMSS/

  lint_issue/                 # Lint issue runs (per specimen + issue + occurrence)
    specimen:{project}/{date}/
      {issue_id}/
        YYYYMMDDTHHMMSS/
          input.json          # IssueCore + Occurrence
          output.json         # LintSubmitPayload
          events.jsonl        # Agent transcript
```
