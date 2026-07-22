# Haku — SPEC

What Haku promises today. Implementation lives where the code lives (`README.md` →
_Where things live_); not-yet-built work and open design questions are `PLAN.md`.

Haku runs in the background of the operator's life with a bundle of (mostly read-only)
access, continuously looking for useful things to do across everything it can see —
Gmail, Calendar, Drive, Tana, Plaid, the cluster, repos, and more as they're wired. It
acts autonomously where safe and read-only (scanning, cross-referencing, research,
synthesis) and surfaces concise, value-ranked recommendations in its own UI; approving
one means **handing it off** (e.g. a prepared prompt taken into a Claude scaffold that
does the work under its own permissions). Open-ended execution with external effects remains a
later direction (`PLAN.md` → _Future_); narrowly reviewed, server-bounded capabilities may be
autonomous exceptions.

The `haku_sandbox` exception gives authenticated Haku Agents approval-free disposable compute,
not new authority over an external system: an Agent may reserve from Haku's dedicated Kubernetes
warm pool, run argv commands capped at five minutes, and inspect health or expiry. Each successful
exec extends an eight-hour sliding lease. Expiry releases the compute while remaining observable to
the caller for a bounded tombstone period.

The narrow `hostexec` exception is always operator-approved: node daemons initiate an
outbound authenticated session to haku-console, and Settings surfaces their heartbeat-derived
connection state. A daemon routing credential cannot authorize execution; every command still
requires the approving operator's short-lived per-host Authentik authority.
