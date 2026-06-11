# Agent Implementations

This directory contains code that runs **inside agent containers only**.

Code here is packaged into OCI images and executed in isolated Docker containers.
It should NOT be imported by orchestration, backend, or other host-side code.

## Structure

- `critic/` — Critic agent (finds issues in code)
- `grader/` — Grader agent (matches issues to ground truth)
- `critic_dev/` — Meta-agents that develop critics
- `docs/` — Agent-facing documentation templates

## Boundary Enforcement

Bazel tests in `props/BUILD.bazel` verify that host-side code (`orchestration/`,
`backend/`, `core/`) does not depend on this directory.
