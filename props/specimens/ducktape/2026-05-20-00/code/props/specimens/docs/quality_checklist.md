## Quality Checklist

Before committing a snapshot, verify all of these criteria:

### Structure & Organization

- [ ] **Snapshot slug format**: Directory path follows `{project}/{YYYY-MM-DD-NN}` pattern (e.g., `ducktape/2025-11-20-00`). Date is snapshot creation date, NN is zero-padded sequence number for that day
- [ ] **BUILD.bazel exists**: `BUILD.bazel` in snapshot directory calling `specimen_targets()` with `slug` and `split`
- [ ] **Issue files location**: All issues in `{project}/{slug}/issues/*.yaml` directory
- [ ] **Slug-based naming**: Issue files use descriptive slugs (e.g., `dead-code.yaml`, `inline-vars.yaml`), not numerical indices. **Prefer shorter names** - `walrus-operator.yaml` not `walrus-operator-opportunities.yaml`. Slugs 0-30 chars, lowercase with hyphens
- [ ] **One logical issue per file**: Each `.yaml` describes ONE logical problem type
- [ ] **Same issue, one file**: If the same issue occurs multiple times (e.g., "upgrade to new syntax"), all occurrences are in ONE shared issue file with multiple entries in `occurrences`

### Issue Quality

- [ ] **No open questions**: All research completed (no "Check if X works" or "TODO: investigate")
- [ ] **Objective descriptions**: No subjective phrasing ("nice pattern", "user mentioned")
- [ ] **Proper structure**: Uses correct YAML structure with `rationale`, `should_flag`, and `occurrences` fields
- [ ] **Brief code citations**: No long code blocks (>10 lines), reader can look up details. Use brief verbal descriptions when sufficient
- [ ] **Proper grouping**: Issues grouped by logical problem, not by location
- [ ] **Accurate line ranges**: Line ranges verified using `adgn-properties snapshot exec <slug> -- sed -n '<start>,<end>p' <file>` to ensure cited lines match what the issue describes
- [ ] **Calibrated rationale**: Detail level matches obviousness (see authoring_guide.md §7). Obvious Pareto improvements (dead code, wrong types, stdlib replacements) need only 1-2 sentences. Complex tradeoffs or judgment calls need substantiation
- [ ] **No unnecessary justification**: Don't explain why types/less-code/safety are good—everyone knows. Only justify when there's a tradeoff or the improvement is non-obvious
- [ ] **Concise rationale**: Rationale is between 10-5000 characters (after whitespace stripping). If over limit, trim unnecessary detail or reconsider if this is actually multiple distinct issues
- [ ] **Verifiable external references**: External code/API/package references include verifiable links (docs URLs, GitHub permalinks with SHAs, package versions)
- [ ] **Snapshot-only references**: Rationale only references the repo state in the snapshot (no historical context or external state required)
- [ ] **Standalone issues**: Each issue YAML file is self-contained and understandable without access to other issue files or non-captured files
- [ ] **No cross-issue references**: Never reference other issues by name, ID, or number (e.g., "See Issue 037", "Related: Issues 036, 038"). Incorporate necessary context directly

### True Positive Issues

- [ ] **should_flag: true**: True positives have `should_flag: true`
- [ ] **critic_scopes_expected_to_recall**: Single-file issues auto-infer; multi-file issues have explicit `critic_scopes_expected_to_recall`
- [ ] **Detection standard applied**: Each file set in `critic_scopes_expected_to_recall` passes the test: "If a high-quality critic reviewed these files (including following imports, searching for patterns, etc.), would failing to find this issue be a failure on their part?"
- [ ] **match_file_restriction validated**: If set, passes the validation test: "Can you produce a valid critique phrasing that accurately describes this issue but tags a file outside the set?" If yes, the set is too narrow. When unsure, use NULL (omit the field)
- [ ] **Multi-occurrence notes**: Issues with multiple occurrences have a `note` for each occurrence
- [ ] **Problem code only**: For "absence of use" issues, include only code that needs to change (violators), not reference/solution code (helpers, fixtures, base classes, constants, patterns, etc.) - unless the solution itself is broken

### False Positive Issues

- [ ] **should_flag: false**: False positives have `should_flag: false`
- [ ] **relevant_files**: FPs have `relevant_files` (auto-inferred from `files` keys, or explicit if needed)
- [ ] **Clear rationale**: Explains why this is NOT an issue (intentional, acceptable pattern, etc.)

### YAML Style

- [ ] **Multi-line rationale**: Use `|` for multi-line rationale strings
- [ ] **Line ranges**: Always use `list[[start, end], ...]` format. Single lines as `[N, N]`. No bare integers, no inline ranges
- [ ] **Minimal comments**: Prefer structured fields over comments
- [ ] **Valid syntax**: All YAML files parse without errors
- [ ] **Occurrence IDs**: Each occurrence has a unique `occurrence_id` (e.g., `occ-0`, `occ-1`)

### Frozen Snapshot Principle

- [ ] **No resolution status**: Issue files don't track "COMPLETED" or "Fixed in commit X"
- [ ] **Historical accuracy**: Issues describe problems as they existed at the snapshot commit
- [ ] **Immutable**: Snapshot remains unchanged after creation (fixes go on separate branches)

### Source Integration

- [ ] **File paths match source**: File paths in issues match the hydrated source structure (include directory prefixes if applicable)
- [ ] **File size reasonable**: No files >2MB in hydrated snapshot
