# Propagation checklists

When information changes in a source, it usually belongs on more than one of my surfaces — and
the failure mode is silent (note it one place, forget the others). Each file here is a checklist
for one change-domain: the surfaces I must consider when something in that domain changes.

**Each run, for each set of changes I saw, I walk the relevant checklist(s)** — for each surface,
decide whether it needs the new info, and record the verdict (including "considered, no change")
in that run's manifest (`runs/<date>/<ulid>.yaml`: `checklists[]` + `propagation[]`).

These are a **FLOOR, not a ceiling.** They enumerate the surfaces I must not forget; free-form
sources (a Gmail thread, a Tana note) carry meaning no checklist can pre-list — apply judgment
beyond them, always. **Keep them current:** a new surface that isn't on a checklist is a surface
I'll eventually forget to update. (CI globs `procedures/propagation/*.md`, so adding a file makes
it a required checklist next run.)
