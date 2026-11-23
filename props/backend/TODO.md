# Props Backend TODO

## Type Safety & Architecture

### Low Priority

- [ ] **Audit JSONB columns for Pydantic conversion** (`props/db/models.py`)
  - Review `Mapped[dict[str, Any]]` JSONB columns
  - Use `PydanticColumn` where structure is known and stable
  - Keep `dict[str, Any]` only for truly arbitrary JSON

- [ ] **Monitor core/API schema drift**
  - Establish pattern for when API schemas should diverge from domain models
  - Currently `LocationAnchor` (`props/db/snapshots.py`) is shared between DB storage and API responses - watch for divergence

## Active Features

### Lower Priority

- [ ] **Stats display improvements**
  - Move total available count to subheader: "Valid Partial (N=171)" instead of "5/171" per row

- [ ] **Live rollout display**
  - Grid of validation jobs with progress bars
  - Timeline of runs (critic->grader pairs)

## Future

- [ ] **Definitions browser:** filter by agent type and show tarball contents on the definition detail page.
