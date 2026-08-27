---
name: pave
description: >-
  Iteratively pave a fiddly procedure (deploys, recoveries, env setup, vendor
  APIs) into a verified runbook: execute the draft, observe friction, amend,
  repeat until a clean run needs zero improvisation. Trigger on "pave this"
  or "let's figure out and document X".
---

# Pave a Runbook

You're producing a runbook for a procedure that is **fiddly** — multi-step, with
rough edges, vendor quirks, or ordering dependencies that aren't obvious from
documentation alone. The output is a document (and possibly small helper
scripts) that someone — you in a future session, the user, another agent — can
follow verbatim with no improvisation.

This is **not** "research → write." Prose written from docs alone bakes in
untested assumptions. The bar is: **a fresh execution, by someone reading only
the runbook, runs cleanly with no surprises.** You can only confirm that by
walking the path yourself, more than once.

## Negotiate the test protocol first

Before any execution, **propose how you will exercise the draft and get the
user's approval.** Fiddly procedures usually need nontrivial environments —
credentials, test accounts, VMs, throwaway clusters, sandboxed networks — and
the user has information you don't:

- what blast radius is acceptable (read-only against the real account vs. a
  throwaway)
- what credentials/access they're willing to share, and in what form (session
  cookie, scoped API token, saved page dumps)
- what hardware/budget is available (can you spin up a Mac VM? a Hetzner box?
  burn $5 of API credits?)
- whether destructive steps must be skipped, mocked, or run for real

Look at the actual environment first (OS, available tools, existing accounts in
the repo) and tailor concrete options to it. State the plan, list the
tradeoffs, ask. Examples:

> You want to pave scraping past AliExpress orders. Two options: (a) hand me a
> session cookie and I'll curl read-only endpoints — fastest iteration, but the
> cookie is a live credential; (b) save 3–4 order pages to disk and I'll
> scaffold v0 against those — slower first cycle but no live credential
> exposure. Which do you prefer?

> You want to pave bootstrapping k8s on colima inside a Mac VM. This is a Mac
> host. My plan: I'll spin up a throwaway tart VM per iteration so the host
> stays clean, then destroy it after each cycle. ~2 min per cycle, ~20 GB disk
> while a VM is alive. OK to proceed?

Don't start cycling until the protocol is agreed. If the protocol turns out to
be wrong (e.g., the read-only-cookie option can't actually exercise the step
that matters), stop and renegotiate — don't silently escalate scope.

## The loop

1. **Execute the current draft as written.** No improvisation, no "I know this
   step, I'll skip ahead." If the draft is ambiguous, that itself is a finding
   — stop and clarify the doc, don't fill the gap from memory.
2. **Note every place you had to think.** Anything that wasn't a literal
   copy-paste of what the doc said, anything you got wrong on first try,
   anything that surprised you, every workaround.
3. **Amend the draft to remove that friction.** A more precise step, a
   copy-pastable command, an explicit gotcha callout, or — if the same fiddly
   step recurs — a small helper script the runbook calls.
4. **Repeat from step 1**, on a fresh environment if possible, until a full
   pass requires zero improvisation.

## When to extract a small tool

If the same multi-line incantation, JSON munging, or sequencing dance shows up
in two consecutive iterations, write a script and have the runbook call it.
Prose that says "now extract the `id` field from the response and pass it as
the next `--parent` argument" is a smell — extract it.

Keep the script next to the runbook (or in the obvious place for the project).
Reference it by path; don't inline its contents into the prose.

## Done criteria

The runbook is paved when **either**:

- A fresh run, ideally on a fresh environment, follows the doc verbatim with
  zero improvisation; or
- Remaining rough edges are explicitly listed as known limitations, with the
  workaround documented inline.

A single clean run after several rocky ones is not done — that's coincidence.
Re-run on a fresh environment, or have the user run it, before declaring
victory.

## Anti-patterns

- **Writing the whole runbook before the first execution.** You don't know
  what's fiddly until you hit it.
- **Smoothing prose without re-running.** Re-reading what you wrote doesn't
  catch ordering bugs, missing flags, or stale URLs.
- **Treating one clean run as proof.** The first clean run usually piggybacks
  on state left from earlier failed runs (warmed caches, half-created
  resources, shell history). Reset.
- **Skipping the protocol negotiation.** Burning credits or touching live
  accounts without approval — or asking the user to set up an environment
  they didn't expect to.
- **Letting the runbook drift from reality.** If you discover a step is wrong
  mid-execution, fix the doc _now_, not "after this run."
