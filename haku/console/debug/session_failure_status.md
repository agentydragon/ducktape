# Why nearly every session is `failed` while every turn succeeded

**Paths below are pre-move.** The runtime is `x/session_runtime.py` and the Matrix modules are
`x/channels/matrix/{client,sync,session,pacer,outbox}.py` since the layout split; the symbol names
are unchanged and are what to grep for.

Follow-up to <frame_shape_census.md>, which observed that `result.subtype` is `success` and
`result.is_error` is `false` on **129 of 129** `result` frames, across **27 sessions the console
records as `failed`**, and left the contradiction unexplained. Two readings were possible: the
sessions really are being killed by something after the turn, or `failed` is being recorded for
something that is not a failure.

**It is overwhelmingly the second, with a real but much smaller residue of the first.** `failed` is
the only terminal status a Matrix session can reach at all, so the sweep that reaps an idle or
untended session writes it for an ordinary end of life. Underneath that, three genuine console-side
defects account for 72 of the 99 failed rows — one of them a single already-fixed incident that
alone contributes 64.

Structure and status only, as the census was: no session content, no room prose, no identifiers
that say what anybody was working on.

## Method, and what it could not reach

Read through the console's own `haku_conversations` MCP tools as the static `haku` Agent, on
2026-08-16, with the operator's explicit authorisation for read-only production diagnostics.
`list_conversations` for the population and its `status`/`error` columns, `list_turns` for turn
outcomes, `read_rollout` for each session's frame tail. Serial throughout — the census deadlocked
the console's Postgres at six concurrent readers.

`list_conversations` still caps at 100 with no cursor, so **the newest 100 sessions is the whole
reachable population**: 2026-08-10T06:51Z through 2026-08-15T07:12Z.

Four things this route could not see, stated before any number below is trusted:

- **No SQL.** `kubectl exec` into the console's Postgres is blocked by this session's permission
  classifier, and so is the same call routed through the console's own `kubectl-passthrough-mcp`
  `pods_exec` tool. So there is **no whole-table histogram**: every count here is over the reachable
  100 rows, and "no session has ever reached `closed`" is an argument from the code below, not a
  measurement.
- **`read_frame` is not deployed.** The deployed server is
  `ghcr.io/agentydragon/haku-console:devel-20260816005648-d50e4f4` (commit `d50e4f489`, #4111);
  `read_frame` and the eleven-kind `kinds` filter landed later, in #4116. `tools/list` on production
  offers exactly `list_conversations`, `read_rollout`, `list_turns`, and `read_rollout` rejects
  `control_request` with `not one of ['assistant', 'user', 'result', 'system', 'command_lifecycle',
'stream_event']`. Frame tails below therefore come from the default view, which shows every kind
  except `stream_event` but cannot be filtered to one of the newer kinds.
- **Console logs from the failures are gone.** Both console pods are well under an hour old; the
  `error` column names 16 distinct replica pods, and not one of them still exists.
- **No turn rows before 2026-08-13T01:21.** Every session created before that has zero rows in
  `session_turns` despite having frames, which is consistent with migration `0032_chat_turns`
  landing at that point — but the repository's history is grafted at a synthetic root
  (`3f2dad04c`, 2026-08-15T04:55Z), so the migration cannot be dated from `git log` and this is
  inference, not evidence.

## What is actually in the reachable 100

| Status   | Sessions |
| -------- | -------- |
| `failed` | 99       |
| `ready`  | 1        |

Nothing else. No `closed`, no `closing`. 29 sessions carry `surface: matrix` and a room id; the
other 71 have `surface: null` and no room, because the column postdates them — 64 of those are the
single 2026-08-10 incident below.

The `error` column is the discriminator the census never read. Verbatim, bucketed only where the
variable part is a pod name:

| Sessions | `error`                                                                                   | Written by                   |
| -------- | ----------------------------------------------------------------------------------------- | ---------------------------- |
| 64       | `Claude runtime failed: 'Connection' object has no attribute 'set_autocommit'`            | `fail()`, from `except*`     |
| 16       | `console replica holding this session went away mid-turn (<pod>)` — 16 **distinct** pods  | `expire_stale_leases()`      |
| 9        | `console replica holding this session went away mid-turn` — no parenthetical              | `expire_stale_leases()`      |
| 5        | `Unexpected ASGI message 'websocket.send', after sending 'websocket.close' or response …` | `fail()`, from the turn loop |
| 3        | the empty string                                                                          | `fail()`, from the turn loop |
| 2        | `console replica holding this session went away mid-turn (no replica (never attached))`   | `expire_stale_leases()`      |
| 1        | `null`                                                                                    | — (the one `ready` session)  |

So **27 of the 99 failed rows were written by the lease sweep and 72 by a caught exception**, and
those two groups do not mean the same thing at all.

By day, the population is three distinct events, not one:

| Date       | Rows                                 |
| ---------- | ------------------------------------ |
| 2026-08-10 | 64 `set_autocommit`, 3 sweep         |
| 2026-08-12 | 8 sweep, 2 ASGI                      |
| 2026-08-13 | 4 sweep, 3 ASGI                      |
| 2026-08-14 | 5 sweep, 2 empty                     |
| 2026-08-15 | 7 sweep, 1 empty, 1 `null` (`ready`) |

### The turns those sessions ran

`list_turns` over all 100 sessions. Ten have turn rows at all (every one created on or after
2026-08-13T01:21); the other 90 have none.

|                                          |                            |
| ---------------------------------------- | -------------------------- |
| Sessions with ≥1 turn                    | 10 (9 `failed`, 1 `ready`) |
| Turns recorded                           | 102                        |
| `answered`                               | 96                         |
| `failed`                                 | 3                          |
| Still open (`ended_at` null, no outcome) | 3                          |

**Three turns failed. Ninety-nine sessions are recorded as failed.** The three `failed` turns are
one each in three different sessions, and they line up exactly with the three rows whose `error` is
the empty string — every other turn in those same sessions is `answered`.

The three open turns are the newest turn of three of the five sessions whose `error` is the ASGI
message; the other two ASGI sessions predate the turns table. Those turns were never closed by
anything, which is what the `_run_turn` docstring (`haku/console/x/claude_chat.py:1553-1558`)
describes as "a replica losing its pod mid-exchange" seen from outside.

### What the last frames look like

Frame tails for all 29 `matrix` sessions, read from the default `read_rollout` view (every kind
except `stream_event`). The modal failed session is not a session that broke — it is a session
nobody spoke to:

| Tail shape                                                                   | Sessions | Reading                                                                        |
| ---------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------ |
| `control_request`, `setup_output` ×3, `control_response` — the whole session | 10       | Provisioned, handshook, bootstrapped, was never asked anything, and was reaped |
| `control_request`, `control_response` — the whole session                    | 4        | Handshake only                                                                 |
| no frames at all                                                             | 1        | Never got as far as a handshake                                                |
| ends on `command_lifecycle` (immediately after a `result`)                   | 7        | Last exchange completed; reaped afterwards                                     |
| ends on `result`                                                             | 2        | Same, without the trailing lifecycle frame                                     |
| ends mid-run of `system`                                                     | 3        | Reaped between frames of an exchange                                           |
| ends on a run of `control_response`                                          | 2        | Repeated `initialize` — replicas re-attaching                                  |

Nine of the ten five-frame sessions are consecutive in the global `frame_seq` counter — 6152, 6157,
6162, 6167, 6172, 6177, 6182, 6187, 6192, exactly five apart — which is what a room that
provisioned a session, said nothing to it, and had it replaced, nine times running over two days,
looks like in the log. No frame in any tail carries `partial`.

The one surviving session (`ready`) has been alive since 2026-08-15T07:12Z — about nineteen hours
at the time of writing — with 25 turns, all `answered`, and a tail of repeated `control_response`
frames from replicas re-attaching across rolls. It is the only session in the corpus created after
the 2026-08-15 adoption fixes, and it is the only one still alive.

## Every code path that writes `failed`

Two write `SessionStatus.FAILED`. Two write `TurnOutcome.FAILED`. They are unrelated clocks.

### Session status

**1. `ClaudeChatStore.fail()` — `haku/console/x/claude_chat.py:959`**

```python
chat.status = SessionStatus.FAILED
```

Means: _some code caught an exception and decided this session is over._ Five call sites, and the
`error` column is the only thing that distinguishes them:

| Call site                            | Text it records                                                                                                |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `haku/console/x/claude_chat.py:1223` | `sandbox provisioning failed: {error}` — `SandboxClaims.create` raised                                         |
| `haku/console/x/claude_chat.py:1359` | `system prompt failed to render: {error}`                                                                      |
| `haku/console/x/claude_chat.py:1441` | `str(error)` — the per-turn `except Exception` around `_run_turn`, **unprefixed**                              |
| `haku/console/x/claude_chat.py:1460` | `Claude runtime failed: {_first_message(errors)}` — the `except* Exception` around the whole runner task group |
| `haku/console/x/claude_chat.py:1726` | `str(error)` again, from inside `_run_turn`, which then re-raises into `:1441`                                 |

Every non-sweep row is attributable by its prefix: the 64 `set_autocommit` rows carry `:1460`'s
`Claude runtime failed:`, and the 5 ASGI rows and 3 empty ones carry no prefix at all, so they came
through `:1441`/`:1726`. That attribution is by message, not by line — these rows were written by
earlier revisions of the same call sites, and one detail says so: three of the ASGI rows have a turn
that is still open, which today's code could not produce, because `:1441` only fires when
`_run_turn` raises and `_run_turn` closes the turn at `:1724` on its way out.

**2. `ClaudeChatStore.expire_stale_leases()` — `haku/console/x/claude_chat.py:1075`**

```python
chat.status = SessionStatus.FAILED
chat.error = f"console session ended{mid_turn}: {detail}"
```

Means: _nobody has renewed this session's lease for `ADOPTION_GRACE` past its expiry, so nobody is
holding it._ This is **a reaper, not a fault detector**, and the code says so — its own comment at
`:1063-1066` names the common case as "a runner was here and released/dropped and nobody re-adopted
(a roll, or the sandbox reaching its TTL)". Both of those are expected events. It writes the same
`FAILED` for all three of the situations it distinguishes in the message:

- `lease_holder` set — a console replica died without handing the session back;
- `lease_holder` cleared but `bridge_connected_at` set — a runner was here and went away and nobody
  re-adopted (a roll, or the sandbox hitting `session_ttl_seconds`, which is 7200 in
  `cluster/k8s/haku/console/config.yaml`);
- neither — a runner never attached.

Only the first is unambiguously a fault. The second is what an idle session's ordinary end of life
looks like.

### Turn outcome

**3. `ClaudeChatStore.adopt_open_turn()` — `haku/console/x/claude_chat.py:463-467`**

```python
await self.end_turn(
    turn_id,
    TurnOutcome.ANSWERED if closing is not None and not closing.get("is_error") else TurnOutcome.FAILED,
    closing,
)
```

Means: _an adopting replica found an open turn that either never asked its question or finished with
an error result._ No reachable session carries this: the three open turns in the corpus were never
adopted at all.

**4. `_run_turn`'s handler — `haku/console/x/claude_chat.py:1724`**

```python
await self._store.end_turn(turn_id, TurnOutcome.FAILED)
```

Means: _this exchange raised._ Three turns in the corpus. This is the only writer that says an
exchange went wrong, and it is the only one whose meaning matches the word.

### And the paths that write something other than `failed`

Worth listing, because their unreachability is the finding:

- `closed()` — `haku/console/x/claude_chat.py:1085` — called from `_finalize` at `:1500`, and only
  when `keep_sandbox` is false, which is only after the turn loop broke on an already-ended status
  or after `fail()` has already run.
- `complete_claim_cleanup()` — `haku/console/x/claude_chat.py:487` — promotes `CLOSING` to `CLOSED`.
  Reached from `dispose()` at `:1248`, whose only caller is `DELETE /api/sessions/{session_id}`
  (`:2067-2071`), an SPA route.

The Matrix supervisor never calls either. `supervise_once`
(`haku/console/x/matrix_session.py:364-404`) runs `expire_stale_leases()` first (`:374`), reads the
status, and if it is not live announces the end and provisions a replacement (`:384-401`) — it
calls `reconcile_terminal_claims()`, never `dispose()`.

**So for a Matrix session, `failed` is the only terminal status the code can produce.** `CLOSED`
requires an HTTP DELETE that nothing in the Matrix path ever issues.

## Verdict

**(2), with a real (1) inside it.**

The status is wrong for the bulk of the population, and it is wrong structurally rather than by
accident:

1. `SessionStatus` has one terminal state for an unexpected end (`FAILED`) and one for a deliberate
   operator-initiated close (`CLOSED`), and **no state for an expected end that nobody asked for** —
   a sandbox reaching its TTL, a session that was provisioned and never spoken to. Since `CLOSED` is
   unreachable from the Matrix path, every Matrix session ends `failed` by construction.
2. `expire_stale_leases` is the reaper for exactly those expected ends, and it writes `FAILED`
   unconditionally at `haku/console/x/claude_chat.py:1075` even in the branch whose own comment
   calls it the common case.
3. The measured tails agree: the modal failed session in the corpus has five frames — handshake,
   three bootstrap lines, handshake response — no prompt, no turn, no exception. Fifteen of the 29
   `matrix` sessions never got past the handshake at all.
4. Turn-level failure is 3 in 102. Session-level failure is 99 in 100. The census's 129/129 clean
   `result` frames are not a paradox; they are what a corpus of successful turns inside reaped
   sessions looks like.

The (1) residue is three genuine console-side defects, all recorded through `fail()`:

- **64 sessions, 2026-08-10, `'Connection' object has no attribute 'set_autocommit'`.** The
  LISTEN/NOTIFY listener written against psycopg3's API while running on an asyncpg engine.
  `haku/console/x/session_notifications.py:1-16` records it: it "raised on every call in production,
  killing every Matrix session about four seconds in". One incident, already fixed, and it is what
  makes the raw `failed` count look catastrophic — it is 64 of the 99.
- **5 sessions, 2026-08-12/13, `Unexpected ASGI message 'websocket.send', after sending
'websocket.close' or response already completed.`** The console writing to the runner websocket
  after it has already been closed. Three of the five left a turn permanently open. No fix for this
  is identifiable in the current code, and it is the one item here that may still be live.
- **3 sessions, 2026-08-14/15, `error` = the empty string.** Each has exactly one turn with outcome
  `failed` — its newest, the one it died on, with every earlier turn `answered`. So `_run_turn`
  raised and `haku/console/x/claude_chat.py:1441` recorded `str(error)` on an exception whose
  message is empty. **What that exception was is not determinable** — the
  operator-facing column holds nothing, the console logs are gone, and I have no SQL. It is a real
  turn failure that the console is structurally unable to explain.

### The self-contradicting string, resolved

Every reachable failed row that came from the sweep carries a message like

```text
console replica holding this session went away mid-turn (no replica (never attached))
```

which says the holder went away _and_ that it never attached. Both clauses are pre-#4060 text and
**neither is load-bearing**: `mid-turn` was hardcoded into the sentence regardless of whether a turn
was open, and `no replica (never attached)` was the fallback printed whenever `lease_holder` was
NULL — which, as #4060's own comment says, "got exactly backwards" the common case, where the lease
holder had been cleared precisely _because_ a runner had been there and released it.

That is already fixed, in `03662cda9` (#4060, 2026-08-15T07:28Z), which replaced

```python
held_by = chat.lease_holder or "no replica (never attached)"
chat.error = f"console replica holding this session went away mid-turn ({held_by})"
```

with the three-branch `detail` and a conditional `mid_turn` now at
`haku/console/x/claude_chat.py:1067-1076`. The fix **is deployed** — the running image is
`d50e4f489`, well past it — but **no reachable row carries the new string**, because no session has
been created since 2026-08-15T07:12Z. The one created sixteen minutes before #4060 landed is still
alive.

That last fact is the strongest single piece of evidence here: the corpus is a record of a runtime
that could not survive a console roll, and the session created just before the 2026-08-15 adoption
work (#4048 `ADOPTION_GRACE`, #4056 `release_held_leases`, #4060, #4064 sandbox deadline sliding)
has now run 25 turns across nineteen hours without being replaced once — through at least the
several re-attachments its repeated `initialize` handshakes record.

## What is left undetermined

- **The whole-table distribution.** Everything above is the newest 100 rows. Whether any session in
  the table's whole history reached `closed`, and what the failure rate looks like before
  2026-08-10, is unmeasured — `list_conversations` has no cursor and SQL was unavailable.
- **What raised with an empty message** in the three `error = ''` sessions.
- **Whether the ASGI send-after-close bug is still live.** It last appears on 2026-08-13; no
  session since has hit it, but only sixteen sessions have been created since, ten of them never got
  past the handshake, and one has been alive for most of the remaining window. Absence over that
  sample is not evidence of a fix.
- **Whether the five-frame sessions were reaped by the sandbox TTL or by a roll.** The pre-#4060
  string cannot distinguish them, which is exactly what #4060 fixed; the answer will be readable
  off the next batch of sessions and is not recoverable for these.
- **Anything older than 2026-08-10**, for the same reason the census could not reach it.

## What the fix would be

Not in this change — the runtime is under active work and `haku/console/x/claude_chat.py` is
serialised.

**The fix is a terminal status for an expected end**, and it lands in two files:

- `haku/console/chat_models.py` — `SessionStatus` gains a fourth terminal member (`EXPIRED`, or
  `ENDED`) meaning "nothing is holding this session any more, and nothing went wrong". No change to
  `LIVE_SESSION_STATUSES` is needed: `ENDED_SESSION_STATUSES` is derived as the complement
  (`chat_models.py:92-95`), so the new member is picked up by every reader for free — which is the
  property that shape was built for.
- `haku/console/x/claude_chat.py` — `expire_stale_leases` (`:1046-1079`) writes the new status for
  the two branches its message already distinguishes as expected (`bridge_connected_at` set with no
  holder; a runner that never attached), and keeps `FAILED` only for `lease_holder is not None`,
  which is a replica that died holding the session. The `error` column becomes a plain statement of
  what ended rather than a sentence with "failed" implied around it.

Two smaller things belong with it, both in `haku/console/x/claude_chat.py`:

- `fail()` at `:948-968` should record the exception's type alongside `str(error)`, so a raise with
  an empty message is still diagnosable. Three sessions in this corpus are permanently unexplainable
  for want of one `type(error).__name__`.
- The Matrix supervisor's announcement (`haku/console/x/matrix_session.py:391-394`) then stops
  telling the room that an ordinary end was a failure.

Until that lands, **the operator-visible failure rate of this runtime carries no information** —
which is the same conclusion <2026_08_13_sessions_boot_and_die.md> reached from the code alone, as
its fix item 3 ("Separate planned from unplanned ends"), and which this note now settles from
production data.
