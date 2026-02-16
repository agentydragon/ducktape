# Specimens Dataset - Agent Guide

@README.md

This directory contains labeled code quality specimens used by the props evaluation system (`props/` in the ducktape monorepo).

## Purpose

You are working with a **dataset of frozen code states with labeled quality issues**, used for training and evaluating LLM code review critics. Each specimen is:

- A snapshot of real code at a specific commit
- Annotated with True Positives (real issues) and False Positives (acceptable patterns that look wrong)
- Immutable training data (like ImageNet for code review)

## Context

This is the **data directory** within the props system. Related components:

- **Props system**: `props/` (parent directory)
- **Training strategy**: <../docs/training_strategy.md>
- **Validation test**: `bazel test //props/core:test_production_specimens`

**Your role**: Authoring, maintaining, or understanding the specimen dataset format.

## Key Documentation

When working with specimens, read these documents in order:

### 1. Format Specification

@docs/format-spec.md

Technical reference for:

- `manifest.yaml` schema (per-snapshot metadata)
- YAML issue file format
- Data models and validation rules

### 2. Authoring Guide

@docs/authoring-guide.md

How to write good specimens:

- Detection standard for `critic_scopes_expected_to_recall`
- Issue organization principles
- Research-first approach
- Objectivity in descriptions
- Code citation guidelines

### 3. Quality Checklist

@docs/quality-checklist.md

### 4. Build and Sync

@docs/build-and-sync.md

Pre-commit verification checklist:

- Structure validation
- Issue quality checks
- YAML style
- Frozen snapshot principle

## Common Tasks

### Authoring a New Specimen

1. **Freeze code state**: Choose a commit and determine scope (which files to include)
2. **Create snapshot directory**: `mkdir -p project/YYYY-MM-DD-NN/code`
3. **Add manifest.yaml** in `project/YYYY-MM-DD-NN/manifest.yaml`:
   ```yaml
   source:
     vcs: local
     root: code
   split: train # or valid/test
   bundle: # optional historical metadata
     source_commit: <40-char SHA>
     include:
       - path/to/code/
   ```
4. **Copy source code** to `project/YYYY-MM-DD-NN/code/`
5. **Create issues directory**: `mkdir -p project/YYYY-MM-DD-NN/issues/`
6. **Author issue files**: One `.yaml` file per logical issue type in `issues/` subdirectory
7. **Verify with quality checklist**: @docs/quality-checklist.md
8. **Test loading**: Use adgn.props package to verify it loads correctly

### Updating Existing Specimens

**⚠️ Specimens are immutable once created.** Do NOT update issue files to track resolution or mark "COMPLETED".

If code has been fixed:

- Create a NEW snapshot at the fixed commit
- Keep the old snapshot unchanged (it's training data)

### Understanding Detection Standard

The key question for `critic_scopes_expected_to_recall`: **"If I gave a high-quality critic this file set to review, and they failed to find this issue, would that be a failure on their part?"**

What "reviewing files" includes:

- Reading files thoroughly
- Following imports to check APIs
- Searching codebase for existing helpers/patterns
- Looking for duplication
- All normal code review activities

What it does NOT mean:

- "Reading files in complete isolation without any searches"

See @docs/authoring-guide.md section "Detection Standard for `critic_scopes_expected_to_recall`" for detailed examples.

### Field Semantics: `critic_scopes_expected_to_recall` vs `graders_match_only_if_reported_on`

**`critic_scopes_expected_to_recall`**: TRAINING SIGNAL. Some known files such that IF a critic is shown these files, THEN we want it to catch this issue. NOT exhaustive - does not enumerate all possible detection sources.

**`graders_match_only_if_reported_on`**: GRADING OPTIMIZATION. Restricts which critique outputs can match this occurrence. If set, a critique reporting issues only in files OUTSIDE this set will be skipped during matching (assumed non-match without semantic comparison).

- **NULL** = allow matching from any file. Conservative default when we haven't determined the closed set, OR for genuinely cross-cutting issues.
- **Non-empty set (≥1 file)** = we know the closed set; skip matching if critique's files don't overlap.
- **Empty set** = INVALID. Not allowed.

These are independent concepts:

- An issue might be detectable from file A (`critic_scopes_expected_to_recall: [[A]]`)
- But once detected, it could be validly reported in files A, B, or C (`graders_match_only_if_reported_on: [A, B, C]`)

Example: "agents.py calls agent.abort() which doesn't exist on MiniCodex"

- `critic_scopes_expected_to_recall: [[agents.py]]` - detectable from the call site
- `graders_match_only_if_reported_on: [agents.py, agent.py]` - valid to tag either:
  - agents.py: "This calls .abort() which doesn't exist"
  - agent.py: "MiniCodex is missing abort() that callers expect"

**Validation test for `graders_match_only_if_reported_on`**: Can you produce a valid critique phrasing that accurately describes this issue but tags a file outside the set? If yes, the set is too narrow.

See @docs/only-matchable-labels.md for labeled examples.

## File Organization

```
props/specimens/
├── CLAUDE.md / AGENTS.md           # Agent instructions
├── README.md                       # Dataset overview
├── docs/                           # Format specs, authoring guide, quality checklist
├── hooks/                          # Pre-commit hooks for specimens validation
├── critic_scopes.yaml              # Training example specifications
└── {project}/                      # Project snapshots
    └── {YYYY-MM-DD-NN}/           # Snapshot slug
        ├── manifest.yaml          # Snapshot metadata (source, split, bundle)
        ├── code/                  # Source code (for vcs: local)
        └── issues/                # Issue files directory
            └── *.yaml             # Issue files (one per logical issue)
```

## Integration with Props

The props system loads specimens via database sync:

```bash
# Sync specimens to database
props db sync

# Run validation test
bazel test //props/core:test_production_specimens
```

The system expects:

- `ADGN_PROPS_SPECIMENS_ROOT` environment variable pointing here (set by `props/.envrc`)
- Valid `manifest.yaml` in each snapshot directory
- Issue files in YAML format under `{snapshot}/issues/`

## Conventions

### Naming

- Snapshot slugs: `project/YYYY-MM-DD-NN` (date is creation date, NN is sequence)
- Issue files: descriptive slugs (`dead-code.yaml`, not `issue-001.yaml`)

### YAML Style

- Use `|` for multi-line rationale strings
- Line ranges: `[start, end]` for ranges, bare integers for single lines
- Minimal comments: prefer structured fields

### Issue Organization

- **One logical issue per file**: Group by problem type, not by location
- **Multiple occurrences**: Use multiple entries in `occurrences` list when same issue appears in multiple places
- **Separate problems**: Create separate files even if issues are on adjacent lines

## Specimen Lifecycle

1. **Capture** → Freeze code at commit
2. **Annotate** → Add issue files describing quality problems
3. **Validate** → Run quality checklist
4. **Freeze** → Commit to this repo (immutable)
5. **Sync** → Load into database via `adgn-properties db sync`
6. **Train** → Used by adgn.props for critic optimization
7. **Evaluate** → Measure recall/precision on validation split

## Questions?

- **Format questions**: See @docs/format-spec.md
- **Authoring questions**: See @docs/authoring-guide.md
- **Props system**: See <../README.md>
- **Training strategy**: See <../docs/training_strategy.md>
