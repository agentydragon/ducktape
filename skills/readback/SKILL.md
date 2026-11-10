---
name: readback
description: >
  Paraphrase vague instructions back with context, without executing changes.
  TRIGGER: any time the user says the word "readback".
---

You just received rushed/vague instructions from the user.

Do not implement the requested change yet.

First, independently research the context needed for an informed readback. For a
code change, inspect the relevant code, structure, callers, configuration, and
existing behavior as needed. Use safe read-only operations; do not ask permission
for this research or skip it merely because the request sounds clear. Scale the
investigation to the task and report any remaining unknowns.

Paraphrase back your understanding of what was requested:

- Fill in missing details based on context
- Make implicit assumptions explicit
- Identify any ambiguities or unknowns
- State what you plan to do, step by step

Do not make state-modifying changes while preparing the readback.

Wait for the user to confirm or correct your understanding before proceeding.
