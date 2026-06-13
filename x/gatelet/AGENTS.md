@README.md

# Agent Guide for `gatelet/`

## Environment

Set `DATABASE_URL` env var pointing to a usable database for tests.

## Template Guidelines

Each HTML template begins with a comment describing its intended audience:

- `human admin`
- `LLM`
- `authenticated human admin or LLM`

Pages for LLMs must offer only link-based navigation and avoid forms or interactive elements.
