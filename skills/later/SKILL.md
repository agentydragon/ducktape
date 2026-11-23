---
name: later
description: Record a TODO item persistently in the repo (TODO.md, PLAN.md, or GitHub issue). PRIORITY - execute immediately, preempting any in-progress work. Use when user says "/later <thing>".
---

**Execute immediately.** Do not finish current work first — record the item now, then resume what you were doing.

Record the user's item persistently. Pick the most contextually appropriate destination:

- **Nearest `TODO.md`** — for items scoped to a specific subproject.
- **`plans/`** — for cross-cutting future work that doesn't belong to one subproject.
- **GitHub issue** — for items that benefit from tracking, discussion, or visibility to others. Use `gh issue create`.

When the follow-up concerns one specific code site (or a small, clear set of sites), also leave a concise TODO comment at the relevant code. If an issue was created, link it in the comment so the follow-up is discoverable from the code. Commit the comment through the repository’s normal change workflow.

Do NOT use ephemeral mechanisms (TaskCreate, mental notes, conversation memory). The item must land in a committed file or a GitHub issue. Append concisely — one bullet or short paragraph. Include today's date if the item is time-sensitive. After writing, confirm what was added and where (one line).
