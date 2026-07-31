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

The narrow `hostexec` exception is always operator-approved: node daemons initiate an
outbound authenticated session to haku-console, and Settings surfaces their heartbeat-derived
connection state. A daemon routing credential cannot authorize execution; every command still
requires the approving operator's short-lived per-host Authentik authority.

A tool call waiting on approval can reach the operator when the console is closed. Browsers
enrolled from Settings → Notifications receive a Web Push notification carrying Approve and Deny,
and it is retracted once the call leaves the queue by any route, so a stale notification never
offers a decision that has already been made. The notification changes where the operator is
asked, never who may answer: deciding from one is the operator's own authenticated session
acting on the console's ordinary approval endpoint, so a push in transit authorizes nothing.
