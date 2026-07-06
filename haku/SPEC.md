# Haku — SPEC

What Haku promises today. Implementation lives where the code lives (`README.md` →
_Where things live_); not-yet-built work and open design questions are `PLAN.md`.

Haku runs in the background of the operator's life with a bundle of (mostly read-only)
access, continuously looking for useful things to do across everything it can see —
Gmail, Calendar, Drive, Tana, Plaid, the cluster, repos, and more as they're wired. It
acts autonomously where safe and read-only (scanning, cross-referencing, research,
synthesis) and surfaces concise, value-ranked recommendations in its own UI; approving
one means **handing it off** (e.g. a prepared prompt taken into a Claude scaffold that
does the work under its own permissions). **Haku executing things itself is a later
direction** (`PLAN.md` → _Future_), not the current contract.
