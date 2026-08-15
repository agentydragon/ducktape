# Sessions that record a container boot and then a crash

**Symptom** (owner, 2026-08-13): most Claude chat sessions record nothing but a container boot and
then a crash.

**Status: the mechanism is established from the code; the first cause is not.** This was written
from a session with no cluster access — `kubectl` has no credentials there and the diagnostics MCP
servers need an interactive OAuth flow — so the ranking below is by argument, and the checks that
would settle it are at the end. Nothing here should be treated as confirmed until one of them runs.

## The mechanism: one failure becomes a crashloop, and the crashloop hides the failure

Four facts, each reasonable alone, that jointly produce the symptom:

1. **The bridge credential is single-use.** `ClaudeChatStore.authenticate_bridge` accepts only a
   session with `status == PROVISIONING` and `bridge_connected_at is None`, and sets both on the way
   through. Every later connect for that session is `REJECTED`. The console closes with 1008, but
   it does so before `accept()`, so what the runner actually receives is an HTTP `403` — see
   `NOT_ADMITTED_CODE`, which is why the first reconnect loop keyed on a close code that could
   never arrive.
   [The ownership plan](../../plans/cli_protocol_ownership.md) already names this as the thing
   re-adoption has to change; what it does today is the subject here.
2. **Kubernetes restarts the runner when it exits.** The pod template
   (<../../../cluster/k8s/haku/workspaces/app/sandboxtemplate-haku-claude.yaml>) sets no
   `restartPolicy`, so it is `Always`, and the janitor policy beside it records that the Agent
   Sandbox controller recreates deleted pods.
3. **The runner has no reconnect and no retry.** `run()` connects once; any exception leaves
   `main()` and the process exits.
4. **Nothing ends the session promptly.** `handle_runner` records a reason through `fail()` and
   returns, but the SandboxClaim survives until the supervisor replaces the session — which it does
   only after `expire_stale_leases` fails it, up to `LEASE_TTL` (90s) after the last renewal.

So a disconnect from **any** cause becomes: runner exits → container restarts → connect refused →
exits → restarts, for about a minute and a half, until the sweep fails the session and the
supervisor deletes the claim and provisions a new one.

The restarts are a symptom of the first failure, not a cause, and from outside they are
indistinguishable from it: each one boots a container and dies. That is why the pattern reads as
"boots and crashes" rather than "died once, then was refused nine times".

**The two halves are individually defensible and jointly wrong.** A single-use rendezvous credential
is right for a bridge that is expected to live exactly once. `restartPolicy: Always` is right for a
process that can resume. Together they guarantee that the platform's recovery mechanism runs into a
refusal, every time, by design.

## Why the record is empty

Three places the reason could be, and is not:

- **The CLI's stderr is discarded.** `bridge_websocket_to_claude` opens the process with
  `stderr=subprocess.DEVNULL` (<../../runtime/x/claude_bridge/runner.py>). If `claude` fails to
  start — a rejected credential, a bad flag, a missing binary — the console learns only
  `Claude Code exited with status N`, and the sentence that said why was thrown away in the sandbox.
- **Bootstrap output is not durable.** `prepare_workspace` streams to the pod's stdout and to the
  room as `SetupOutput` progress. It never reaches `claude_chat_frames`, so once the pod is reaped
  the only copy is in the room, interleaved with everything else.
- **The room is told the status and never the reason.** `supervise_once` announces
  `session … ended (failed); starting a new one`, while `chat.error` — which is always set, and is
  specific — stays in Postgres. The console records a precise reason where nobody reads it and
  narrates an imprecise one where everybody is looking.

## The first cause, near-certainly: the console rolls, constantly

The owner's read, and it survives arithmetic. `cluster/k8s/haku/console/deployment.yaml` took **41
image bumps in the seven days to 2026-08-13** — Flux writes the tag back on every build — which is
about **six rolls a day**. Each roll cancels `handle_runner` on the replica holding the bridge,
which records `console replica shut down mid-session`, closes the socket, and hands the runner to
the crashloop above.

Add the twelve daily TTL expiries and a room's session dies roughly **every eighty minutes**, having
usually done nothing in between. That is the whole symptom, with no exotic cause needed: sessions do
not live long enough to be talked to, and the ones that record only a boot and a death are the ones
that were provisioned into a window with no traffic in it.

This was ranked second when the note was written, on the argument that rolls explain "regularly"
rather than "most". Six a day _is_ most, and the measurement settles it.

**Remaining candidates, for the residue.** Worth keeping only because the fix below makes rolls
survivable and whatever is left will then be visible:

- **The bootstrap failing.** `prepare_workspace` is fatal by design — a session without the
  haku-state checkout is a generic assistant, which is worse than no session — and it runs _after_
  the bridge token is consumed, so the runner that hit it can never retry. It clones haku-state with
  credentials from the reflected `haku-forgejo-git` secret, which this repo already warns drifts
  (root `AGENTS.md`, "Haku Forgejo Tokens"). Produces an entirely empty rollout, since it precedes
  any CLI frame.
- **A rate-limited burst of bootstrap narration**, which would fail the session _while reporting
  something else_. The console turns `SetupOutput` into one room notice per line
  (`transport.py` splits the runner's raw chunks and awaits `on_progress` for each), unpaced —
  and `prepare_workspace` merges the bootstrap's stderr into that stream, so the **failure** path is
  the one that produces the burst. Nothing handles a 429: `_unwrap` raises `MatrixError` and drops
  `retry_after_ms`, and the awaited `on_progress` has no guard, so the raise leaves the transport's
  read loop and lands in `handle_runner`'s `except*` as `Claude runtime failed: …`. A bootstrap that
  failed for an ordinary reason would then be recorded as a Matrix error, with its actual message
  three notices back in a room nobody is reading. Distinguishable in the query below: the error text
  says `429`.
- **The CLI exiting on its own**, which would read as `Claude Code exited with status N` with the
  reason discarded by the DEVNULL above.
- **The 7200s TTL** — a death the console itself scheduled, arriving as `WebSocketDisconnect` and
  recorded as `sandbox runner disconnected`, indistinguishable from a real fault.

## The three checks that settle it

```sql
-- 1. What actually killed them, and how long they lived. If most rows are the same
--    error and `lived` is seconds, it is (1) or (3); if `lived` is ~7200s it is (4).
SELECT error, count(*), min(updated_at - created_at) AS lived
FROM claude_chat_sessions WHERE status = 'failed'
GROUP BY error ORDER BY count(*) DESC;
```

```bash
# 2. Whether the pods are crashlooping, and what the runner said before it died.
kubectl -n haku-claude-sandbox get pods -o wide          # RESTARTS is the tell
kubectl -n haku-claude-sandbox logs <pod> --previous     # bootstrap output lives here
```

```sql
-- 3. Whether any session ever got as far as a turn. If `turns` is 0 across the board,
--    the failure precedes the CLI, which points at the bootstrap.
SELECT s.session_id, s.error, count(t.turn_id) AS turns, count(f.frame_seq) AS frames
FROM claude_chat_sessions s
LEFT JOIN claude_chat_turns t ON t.session_id = s.session_id
LEFT JOIN claude_chat_frames f ON f.session_id = s.session_id
GROUP BY s.session_id, s.error ORDER BY s.created_at DESC LIMIT 20;
```

## What to fix, whichever it turns out to be

Ordered so each step makes the next one diagnosable.

1. **Put the reason where it is read.** Announce `chat.error` alongside the status in
   `supervise_once`; stop discarding the CLI's stderr; record `SetupOutput` into the rollout so a
   reaped pod's bootstrap survives. None of these changes behaviour, and without them the next
   occurrence is as opaque as this one.
2. **Make a console roll survivable**, which is the fix for the measured cause and which also
   retires the crashloop, since a runner that reconnects is a runner that never exits. The idle-session
   subset below is small; the mid-turn half is design B in full.
3. **Separate planned from unplanned ends.** The TTL reap and the console roll are both expected,
   and both currently present as `failed` with an alarming string. A session that reached its
   scheduled end is not a failure, and while it is recorded as one, no failure rate means anything.
4. **Then reconsider eager allocation.** See below.

## Surviving a console roll

The wanted end state — the runner holds inbound and outbound queues, redials, and flushes on
reconnect — is design B in <../../plans/cli_protocol_ownership.md>, and the useful thing to add here
is that **the common case needs a small fraction of it**, and that fraction is worth landing on its
own.

**Most rolls land on an idle session.** A room that gets a handful of messages a day is between
turns for almost all of the six daily rolls. With no turn in flight there is nothing in either
queue, and surviving the roll needs only three things:

- the runner stops killing the CLI when the socket closes — `bridge_websocket_to_claude`'s `finally`
  terminates the process, which is the single line that makes the sandbox disposable;
- the runner redials with backoff instead of exiting, which also retires the crashloop, since a
  process that does not exit is not restarted;
- `authenticate_bridge` grows an adopt branch instead of refusing every reconnect, gated on taking
  the lease — that gate is the arbitration that stops both console replicas adopting one CLI.

No sequence numbers, no ring buffer, no `PROTOCOL_VERSION` bump. The CLI is already initialized, and
the runner has to own that fact so the adopting console does not re-handshake a live process — which
is the one piece of design B this subset does need.

**Rolled mid-turn is the harder half**, and it is where the queues earn their place: frames the agent
produced while nobody was listening have to survive, so the runner keeps a bounded outbound buffer
and re-sends from it on adopt. What makes that safe is that the frames worth replaying carry the
agent's own identity, so a duplicate is recognisable and the cursor only has to be roughly right —
worked through in [the ownership plan](../../plans/cli_protocol_ownership.md), including the one
frame class where replay would corrupt rather than duplicate.

Worth noticing that the **inbound half is already durable, on the console side**: `claude_chat_prompts`
is a Postgres queue, so a prompt that was never delivered is not lost. With one gap that adoption
turns from harmless into real — `next_prompt` marks the prompt claimed and opens the turn in one
transaction, and `_run_turn` writes it to the CLI _after_ that. A replica dying in between leaves a
claimed prompt that was never asked and a turn that will never end. Today that hardly matters,
because the session dies with it; once sessions survive, an adopted session with an open turn has to
be able to tell "the prompt was delivered and the answer is coming" from "the prompt never left" —
which is what `command_lifecycle`'s `queued`/`started` says, and another reason the uuid stamping is
already there.

**One cheap thing that is not in design B: say goodbye.** The console knows it is going away — the
`CancelledError` path exists precisely to record it, and the pod has a 30s grace period with a
shielded finalizer inside it. It could close the bridge with a code meaning _rolling, reconnect_
rather than just dropping the socket, so the runner distinguishes a roll from a rejection instead of
inferring. Cheap, and it makes the logs say which of the two happened.

**And six rolls a day is itself worth a look.** If the console image is rebuilt and redeployed on
commits that do not touch the console, most of those rolls are churn — but surviving a roll is the
right fix regardless, because rolls are legitimate and a session that cannot outlive one is a
session that cannot outlive a node drain either.

## The lazy-allocation proposal, revisited

The owner's first instinct was to allocate the sandbox only when there is a prompt to process, which
would stop idle rooms from generating boot-and-die records. That is worth doing on its own merits
(<../../plans/chat_runtime_cleanup.md>), but it is **not the fix for this symptom**, and doing it
first would make the symptom harder to see rather than better: if the first cause is the bootstrap,
the failure simply moves from "always" to "whenever somebody speaks", which is when it costs a
person something. Fix the reason-reporting and the crashloop first; allocate lazily once a session
that is asked for reliably survives being asked for.

## Verifying the fix

Everything above this is diagnosis. The fix landed across stages 1 and 4 and, until the run below,
was **verified only by tests** — which is how it reached production with a hole in it.

**Roll the lease holder with `kubectl delete pod`, not `rollout restart`.** The restart works by
adding a `kubectl.kubernetes.io/restartedAt` annotation to the pod template, which Flux reconciles
away; with `maxUnavailable: 0` the replacement never becomes Ready, so the originals are never
terminated — and `rollout status` reports success anyway. A pod deletion is not a spec change, so
nothing reverts it, and it is exactly the event being tested.

Deleting the _other_ replica proves nothing, so finding the holder is not optional:

```sql
SELECT session_id, status, lease_holder, lease_expires_at
FROM claude_chat_sessions
WHERE status IN ('ready', 'responding', 'provisioning')
ORDER BY created_at DESC;
```

```bash
kubectl -n haku-claude-sandbox get pods       # note RESTARTS: this is the number that used to climb
kubectl -n haku-console delete pod <lease_holder>
```

### A. An idle session (stage 1)

The common case — six rolls a day, almost all between turns.

**Passes when** the `session_id` is unchanged, its `lease_holder` is now a _different_ pod, the
sandbox pod's `RESTARTS` has not moved, and a message in the room is answered.

**Fails as** `status = 'failed'` with `error` containing "console replica holding this session went
away mid-turn", or a new `session_id` where the old one was. That is the original bug: the runner
was refused on reconnect and the supervisor built a replacement.

### B. Mid-turn (stage 4)

The half only a roll exercises, and the newer code.

1. Ask Haku something that runs for a while — a few tool calls, thirty seconds or more.
2. While the room's status line still shows it working, delete the lease holder.
3. Watch the room.

**Passes when** the answer arrives **once**, and

```sql
SELECT turn_id, started_at, ended_at, outcome FROM claude_chat_turns
WHERE session_id = '<id>' ORDER BY started_at DESC LIMIT 5;
```

shows **one** turn covering the exchange with `outcome = 'answered'`.

**Fails as** a turn with `outcome = 'failed'` followed by a second turn for the same prompt — the
turn was closed and the prompt re-asked — or the answer appearing in the room twice.

The adopting replica says so in its log. Its **absence** means the roll landed on an idle session
rather than that stage 4 did nothing; the two are identical from the room, so retry rather than
concluding anything:

```bash
kubectl -n haku-console logs -l app.kubernetes.io/name=haku-console --since=15m \
  | grep -i "adopted with turn"
```

### While you are in there

One number this repo estimates rather than knows — `chat_runtime_projection.md` stage 1 says to
check it before stage 2 relies on it:

```sql
SELECT kind, count(*) FROM claude_chat_frames
WHERE session_id = '<id>' GROUP BY kind ORDER BY count(*) DESC;
```

`stream_event` should be the largest bucket by a wide margin. How wide is the answer to "what does
recording the deltas actually cost".

## What the first real roll found: the fix had a hole

Run 2026-08-15. The deliberate `rollout restart` **did not roll** — Flux reconciled the
`kubectl.kubernetes.io/restartedAt` annotation away mid-roll, and with `maxUnavailable: 0` the
replacement never became Ready, so the original pods were never terminated. `rollout status` still
reported success. **Roll the session's lease holder with `kubectl delete pod` instead**; it is not a
spec change, so nothing reverts it.

The genuine roll fifteen minutes earlier is what settled it. Session `425b2586` died with

> console replica holding this session went away mid-turn (haku-console-dc8b85747-lwd7s)

on consoles running `a9e2118` → `a563ee6` and a runner on `086dc8f` — every side carrying the fix.

**Two causes, and the second is the one that mattered.**

1. `expire_stale_leases` writes `chat.lease_holder or "no replica (never attached)"`. It named the
   pod, so `lease_holder` was still set — `release_lease` never ran on that pod's shutdown. There
   is no `terminationGracePeriodSeconds` on the deployment, so the whole shutdown has 30s.
2. **A held lease and a bad credential were the same answer on the wire.** `authenticate_bridge`
   refused a runner reaching a new replica while the old lease was still valid, and a websocket
   closed before `accept()` reaches the client as HTTP 403 whatever close code is passed —
   uvicorn renders every one that way. `_worth_redialling` gives up on a 4xx, correctly, because a
   bad credential is not worth redialling. So the runner released the sandbox about a second after
   the roll began.

The second is a race that was open even when `release_lease` worked: the runner redials in about a
second, and the dying replica commits its release whenever it gets there.

**Fixed by giving the third answer its own signal.** `BridgeAuthentication.HELD` answers the
handshake **503** through the ASGI `websocket.http.response` extension, which uvicorn advertises and
Starlette exposes as `send_denial_response`. The runner needed no change — `_worth_redialling`
already waits out anything 5xx, because that is also what the Gateway says mid-roll.

**Cause 1 is still open**, and it is now latency rather than correctness. With the release working,
the holder clears immediately and the runner's next retry is admitted. Without it, the lease runs
its full 90s while the sweep (every 10s) is trying to fail the session and the runner retries every
≤20s — a race, in the degraded path only.
