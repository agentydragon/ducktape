# Code Review Specimens Dataset

Labeled code quality specimens used for training and evaluating LLM code review critics. Part of the `props/` evaluation system in the ducktape monorepo.

## Purpose

Specimens are **frozen code states with labeled ground truth issues**, serving as training/evaluation data for behavior-cloning code review agents. Each specimen represents a snapshot of real code at a specific commit, annotated with:

- **True Positives (TPs)**: Real issues that should be caught by a competent code reviewer
- **False Positives (FPs)**: Patterns that look wrong but are actually acceptable (intentional design choices)

Think of this as "ImageNet for code review" - immutable labeled datasets for supervised learning and evaluation.

## Structure

```
props/specimens/
├── defs.bzl                    # specimen_targets() macro
├── docs/                       # Format specs, authoring guide, quality checklist
├── hooks/                      # Pre-commit hooks for specimens validation
├── ducktape/                   # Snapshot directory (one per project)
│   └── 2025-11-26-00/          # Snapshot slug (YYYY-MM-DD-NN)
│       ├── BUILD.bazel         # Metadata: slug, split, code source
│       ├── code/               # Source code (for local specimens)
│       └── issues/             # Issue files directory
│           ├── dead-code.yaml
│           ├── missing-types.yaml
│           └── ...
├── crush/                      # Another project's snapshots
└── misc/                       # Miscellaneous/experimental snapshots
```

## Snapshot Format

Each snapshot directory contains:

- **`BUILD.bazel`**: Metadata via `specimen_targets()` — slug, split, code source. Provenance (commit SHA, include/exclude) as comments.
- **Issue files** (`.yaml` in `issues/` directory): One file per logical issue type
- **`code/`** (optional): Source code for local specimens

## Issue File Format

Issues are authored in YAML for simplicity and readability:

```yaml
rationale: |
  Dead code should be removed. Lines 145-167 define a function
  that is never called anywhere in the codebase.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      src/cli.py:
        - [145, 167]
    # critic_scopes_expected_to_recall auto-inferred for single-file issues
```

Key fields:

- `rationale`: What's wrong and why (objective, factual description)
- `should_flag`: `true` for real issues, `false` for false positives
- `occurrences`: List of occurrence locations with file paths and line ranges
- `critic_scopes_expected_to_recall`: Minimal file sets needed to detect this issue (used for per-file training examples)

See <docs/format_spec.md> for detailed schema documentation.

## Training Strategy

This dataset supports **per-file training examples** for tighter feedback loops during prompt optimization:

- **Training split**: Full access to code, ground truth, and execution traces
- **Validation split**: Can run evaluations, but cannot read labels directly (held-out generalization test)
- **Per-file examples**: Auto-generated from `critic_scopes_expected_to_recall` in issue files

Example: Instead of just "review this entire 50-file snapshot", we generate:

- Single files: "Review `server.py`"
- File pairs: "Review `types.py` + `persist.py`" (check for duplication)
- Component sets: "Review all `*.svelte` files" (UI patterns)

This gives ~100+ training examples from 5 snapshots instead of just 5.

## Usage

Specimens are loaded into the props database via sync. The `ADGN_PROPS_SPECIMENS_ROOT` environment variable (set by `props/.envrc`) points to this directory.

```bash
# Sync specimens to database
props db sync

# Run the specimens validation test
bazel test //props/core:test_production_specimens
```

## Authoring Guidelines

When adding new specimens or issues:

1. **Research first**: Complete all investigation before authoring (no open questions)
2. **One logical issue per file**: Group by problem type, not by location
3. **Objective descriptions**: Describe facts and technical rationale, not opinions
4. **Verify file paths**: Match hydrated bundle structure exactly
5. **Detection standard**: "If a high-quality reviewer saw these files, would failing to find this be a failure?"

See <AGENTS.md> for detailed authoring instructions.

## Dataset Splits

- **train**: For training, optimization, and detailed analysis (readable labels)
- **valid**: For held-out evaluation (can run critics, measure recall, but can't read labels)
- **test**: Reserved for final holdout evaluation (not used during development)

Current split distribution:

- ~5 training snapshots (with per-file scopes → ~100+ training examples)
- ~2 validation snapshots (full-snapshot evaluation only)

## Specimen Lifecycle

1. **Capture**: Freeze code state at specific commit
2. **Annotate**: Add issue files describing all quality problems
3. **Validate**: Verify paths, ranges, and detection expectations
4. **Freeze**: Commit to this repo (immutable training data)
5. **Sync**: Load into database via `adgn-properties db sync`
6. **Train**: Use for critic optimization (GEPA, prompt tuning, etc.)
7. **Evaluate**: Measure recall/precision on validation split

**Important**: Specimens are **immutable once created**. Do not update issue files after fixes are made - create new snapshots if you want to capture improvements.

## Related Documentation

- [Format Specification](docs/format_spec.md): YAML schema and data models
- [Authoring Guide](docs/authoring_guide.md): How to write issue files
- [Quality Checklist](docs/quality_checklist.md): Pre-commit verification
- [Training Strategy](../docs/training_strategy.md): Per-file examples, optimization approaches
