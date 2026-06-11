@README.md

# Agent Guide for `gatelet/`

## Environment

Set `DATABASE_URL` env var pointing to a usable database for tests.

## Codex Environment

If `IS_CODEX_ENV=1` is set, tests must bring up AND tear down the database. No processes may linger between execution steps.

If running in Codex without internet, run `gatelet/setup.sh` from repo root before network access
is disabled. To add dependencies, update `setup.sh` for future runs.

## Testing

```bash
pytest
```

Before committing: `pre-commit run --files <changed files>`

## Template Guidelines

Each HTML template begins with a comment describing its intended audience:

- `human admin`
- `LLM`
- `authenticated human admin or LLM`

Pages for LLMs must offer only link-based navigation and avoid forms or interactive elements.
