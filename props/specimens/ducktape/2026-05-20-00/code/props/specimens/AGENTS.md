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

@docs/format_spec.md

Technical reference for:

- Snapshot metadata (`BUILD.bazel` via `specimen_targets()`)
- YAML issue file format
- Data models and validation rules

### 2. Authoring Guide

@docs/authoring_guide.md

How to write good specimens:

- Detection standard for `critic_scopes_expected_to_recall`
- Issue organization principles
- Research-first approach
- Objectivity in descriptions
- Code citation guidelines

### 3. Quality Checklist

@docs/quality_checklist.md

### 4. Build and Sync

@docs/build_and_sync.md

Pre-commit verification checklist:

- Structure validation
- Issue quality checks
- YAML style
- Frozen snapshot principle

## Common Tasks

### Authoring a New Specimen

1. **Freeze code state**: Choose a commit and determine scope (which files to include)
2. **Create snapshot directory**: `mkdir -p project/YYYY-MM-DD-NN/code`
3. **Create BUILD.bazel** in `project/YYYY-MM-DD-NN/BUILD.bazel`:

   ```python
   load("//props/specimens:defs.bzl", "specimen_targets")

   # Snapshot of {repo} at {commit_sha}.
   specimen_targets(
       name = "specimen",
       code_srcs = glob(["code/**/*"]),
       slug = "project/YYYY-MM-DD-NN",
       split = "train",  # or valid/test
   )
   ```

4. **Copy source code** to `project/YYYY-MM-DD-NN/code/`
5. **Create issues directory**: `mkdir -p project/YYYY-MM-DD-NN/issues/`
6. **Author issue files**: One `.yaml` file per logical issue type in `issues/` subdirectory
7. **Verify with quality checklist**: @docs/quality_checklist.md
8. **Test loading**: Use adgn.props package to verify it loads correctly

### Updating Existing Specimens

**⚠️ Specimens are immutable once created.** Do NOT update issue files to track resolution or mark "COMPLETED".

If code has been fixed:

- Create a NEW snapshot at the fixed commit
- Keep the old snapshot unchanged (it's training data)

### Detection Standard and Field Semantics

Detection standard for `critic_scopes_expected_to_recall` and `match_file_restriction` semantics are covered in the authoring guide and format spec (transcluded above). For labeled examples of `match_file_restriction`, see @docs/only_matchable_labels.md.

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
- Valid `BUILD.bazel` calling `specimen_targets()` in each snapshot directory
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

- **Format questions**: See @docs/format_spec.md
- **Authoring questions**: See @docs/authoring_guide.md
- **Props system**: See <../README.md>
- **Training strategy**: See <../docs/training_strategy.md>
