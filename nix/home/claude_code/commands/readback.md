---
description: Paraphrase vague instructions back with context, without executing changes
---

You just received rushed/vague instructions from the user.

**STOP. Do NOT execute anything yet.**

Paraphrase back your understanding of what was requested:

- Fill in missing details based on context
- Make implicit assumptions explicit
- Identify any ambiguities or unknowns
- State what you plan to do, step by step

You may gather context if needed using safe read-only operations (Read, Grep, Glob, git commands, WebFetch/WebSearch), but skip this if the instructions are already clear.

Do NOT use any state-modifying tools (Edit, Write, MultiEdit, or destructive Bash commands).

Wait for the user to confirm or correct your understanding before proceeding.
