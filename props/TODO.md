# TODO

## Standards

- Potential indexing (property-specimen cross-refs) if/when scale requires it
- Policy: should verbatim docstring repetition in ABC subclass methods violate no-useless-docs? Lean yes, undecided.
- Property naming mismatch: "self-describing names" vs "use datetime for datetimes". Decide scope or split.
- Target Python version detection/guidance for agents/graders

## Features

- Reimplement `fix` command as critic-driven loop: run critic, fix issues, rerun until clean or max iterations
- Agent timeout warning handler: inject "5 minutes remaining" messages using `created_at` + `timeout_seconds`

## Infrastructure

- Sane story for applying migrations without full `db recreate` (direct `alembic upgrade head`)
- Bulk specimen sync in `props db sync` from Bazel bundle artifacts (currently one-by-one via `sync-specimen`)
