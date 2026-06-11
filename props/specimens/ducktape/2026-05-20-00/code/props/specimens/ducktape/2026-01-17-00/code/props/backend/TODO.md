# Props Backend TODO

## Type Safety & Architecture

### Medium Priority

- [ ] **NOTE: FileLocationInfo should stay in props.core**
  - FileLocationInfo is correctly defined in props.core (domain model layer)
  - Backend routes import from props.core - this is correct architecture
  - No changes needed

### Low Priority

- [ ] **Make export.py functions public** (src/props_core/db/sync/export.py)
  - Remove leading underscore from `_format_files`, `_format_line_ranges`
  - Add to `__all__` export list
  - Document as canonical serialization API for reuse

- [ ] **Audit JSONB columns for Pydantic conversion** (src/props_core/db/models.py)
  - Review `Mapped[dict[str, Any]]` JSONB columns (lines 174,181,190,198)
  - Convert to `MappedPydanticJSONB[ModelType]` where structure is known and stable
  - Keep `dict[str, Any]` only for truly arbitrary JSON

- [ ] **Monitor core/API schema drift**
  - Establish pattern for when API schemas should diverge from domain models
  - Currently `LineRange` is shared between core and API - watch for divergence

## Active Features

### High Priority

- [ ] **Add "X runs in progress" indicator**
  - Show separate count/indicator for how many runs are currently in progress
  - Stats already exclude in_progress runs (filtered in recall_by_run view)
  - Frontend already shows full status words (not S/C letters)

### Lower Priority

- [ ] **Stats display improvements**
  - Move total available count to subheader: "Valid Partial (N=171)" instead of "5/171" per row
  - Full 95% CI display: "45.2% [38.1% - 52.3%]" or "45.2% ±7.1%"

- [ ] **Migrate `props stats` to frontend**
  - Tables: by-example, by-occurrence views
  - Include props stats subcommands: `example`, `occurrence`

- [ ] **Live rollout display**
  - Grid of validation jobs with progress bars
  - Timeline of runs (critic->grader pairs)

## Future

- [ ] **Ground truth update workflow**
  - BUG: Staleness check marks everything as stale (compares wrong fields)
  - Fix: Compare only TP/FP IDs, rationales, locations - not `critic_scopes_expected_to_recall`
  - Then: `/api/stats/stale-runs` endpoint, dashboard indicator, regrade button

- [ ] **Definitions browser page**
  - Filter by agent type
  - View definition details (tarball contents)
  - Click through to runs
