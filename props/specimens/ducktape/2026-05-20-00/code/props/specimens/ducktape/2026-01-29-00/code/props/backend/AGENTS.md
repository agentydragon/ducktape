@README.md

# Agent Guide

## Documentation Convention

This project uses two companion files for tracking work:

### TODO.md

**Purpose:** Captures implementation tasks to work on later.

- Edited as things are completed (check off items)
- Organized by priority (High/Medium/Lower)
- Tracks current component and endpoint status
- Living document - update frequently

### SPEC.md

**Purpose:** Evolving specification of the "target desired state" to reconcile to.

- Append-only (don't delete features, only add)
- Describes what features should exist when complete
- Reference for conformance checking
- Includes CLI features to migrate, live display requirements, future extensions

**Workflow:** When implementing, check TODO.md for what to do next and SPEC.md for what the end result should look like.
