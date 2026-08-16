# What each of the session runtime's invariants cost to learn

`x/session_runtime.py` and `x/session_store.py` carried roughly 150 lines that narrated what the
code used to be and which bug that caused. STYLE § Documentation puts historical "used to"
comments and changelog comments under **Remove**, so each of those is now one imperative line in
the code and the story it was carrying is here.

**Nothing below is a statement about how the code behaves today.** Read it to find out _why_ a
line in those two files says what it says; read the code for what it does. Several of these
incidents already had a note of their own, and those rows point at it rather than retelling it —
one incident, one write-up.

## Index — the invariant, and where its story is

| Code site                                                    | The invariant, as the code now states it                                                                  | Story                                                                                                           |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `session_store.enqueue_prompt`                               | No status write: a queued prompt is not a turn in flight                                                  | [A queued prompt that claimed to be a turn](#a-queued-prompt-that-claimed-to-be-a-turn)                         |
| `session_store.BridgeAuthentication`                         | "Not yours" and "not yet" are different                                                                   | <2026_08_13_sessions_boot_and_die.md> § What the first real roll found                                          |
| `session_runtime.handle_runner`, the `HELD` branch           | A denial response, not a close                                                                            | <2026_08_13_sessions_boot_and_die.md> § What the first real roll found                                          |
| `session_store.expire_stale_leases`                          | An expired lease means unowned, not dead                                                                  | <2026_08_13_sessions_boot_and_die.md> § The re-test                                                             |
| `session_store.release_lease`, `release_held_leases`         | A courtesy, not the mechanism — a SIGKILL runs no finalizer                                               | <2026_08_13_sessions_boot_and_die.md> § Why `release_lease` never ran                                           |
| `session_store.expire_stale_leases`, the three `detail` arms | Which of three things ended the session, said in the message                                              | <session_failure_status.md> § The self-contradicting string, resolved                                           |
| `session_runtime._finalize`                                  | `keep_sandbox` is the difference between "this conversation is over" and "this replica is"                | [A roll that deleted the sandbox it was leaving behind](#a-roll-that-deleted-the-sandbox-it-was-leaving-behind) |
| `session_runtime.handle_runner`, the shielded `finally`      | Shielded because everything here is an `await` and this task may already be cancelled                     | [The finalizer that stopped at its first await](#the-finalizer-that-stopped-at-its-first-await)                 |
| `session_runtime._renew_lease`                               | The sandbox is a renewed lease rather than a hard `session_ttl_seconds` timer                             | [The sandbox deadline that did not move](#the-sandbox-deadline-that-did-not-move)                               |
| `session_runtime._run_turn`, the drain                       | The drain is this loop, not a second one beside it                                                        | <message_drops.md> E3                                                                                           |
| `session_store.update_assistant`                             | A completed message queues the room's copy in this same transaction                                       | <message_drops.md> E4                                                                                           |
| `session_runtime._run_turn`, `spoke`                         | The outbox row's existence, never a report from the delivery layer and never `sent_at`                    | <message_drops.md> § The one structural fact, and E2                                                            |
| `session_runtime._run_turn`, `saw_assistant_message`         | Its own fact rather than `spoke` again                                                                    | [Two facts under one flag](#two-facts-under-one-flag)                                                           |
| `session_store.fail`, `handle_runner`'s `except*`            | Logged as well as persisted; the traceback is what says which call produced it                            | <session_failure_status.md> § Verdict, and `x/session_notifications.py`                                         |
| `session_store.ResumedTurn`, `TurnState`                     | Adoption reads how far the turn got off its row, rather than rebuilding it from the frame log             | <../../plans/chat_runtime_projection.md> § stage 3                                                              |
| `session_store.SpaSession.surface_column`                    | What the row records, carried on the variant                                                              | [Small ones](#small-ones)                                                                                       |
| `session_runtime._run_turn`, `result`                        | The frame the completion was projected from, kept because the code below still reads Claude's own payload | <../../plans/chat_runtime_projection.md> § stage 4                                                              |

## A queued prompt that claimed to be a turn

`enqueue_prompt` used to write `status = responding` on the session row as it accepted a prompt.
It reads as bookkeeping — the operator has spoken, the session is about to answer — and it is
wrong by one event: a prompt sitting in `session_prompts` is queued, not running. Nothing had
been sent to the agent and no turn existed.

Everything that asked the session row "is this answering?" therefore got a yes it could not act
on. `request_abort` was the one that mattered: it accepted the operator's abort, published the
`ABORT` notify, and there was no turn loop anywhere to receive it — the interrupt reached a turn
that had not been opened. The operator saw an accepted abort and an answer that arrived anyway.

The fix is the shape the whole file now has: **the open turn is the single fact, and `responding`
is derived from it** rather than stored beside it. `_open_turn` is the one query, and it answers
all three questions that used to read the status column — whether a prompt may be admitted
(`enqueue_prompt`), whether there is anything to abort (`request_abort`), and what the SPA is
shown (`session_view`). `uq_session_turns_open` makes "at most one" a schema property, so the
lookup needs no rule attached to it.

The same reasoning is what `enqueue_prompt`'s admission check is about, and that one is stated in
the code rather than here, because it is a rule a future editor could plausibly undo: gating
admission on `READY` alone would accept a prompt mid-turn, which is mid-turn steering arriving by
accident with no fold path wired (R2.2 holds a batch until the turn ends).

## A roll that deleted the sandbox it was leaving behind

`_finalize` runs on every exit from `handle_runner`, and it originally did the same thing on all
of them: close the socket, close the CLI client, delete the SandboxClaim, mark the session
`closed`. That is correct for exactly one of the reasons `handle_runner` returns.

A console roll cancels `handle_runner` on the replica holding the bridge. The session is not
over — the sandbox outlives the console process, the runner redials within about a second, and
whichever replica answers adopts it. Deleting the claim in that path destroyed the pod that the
adoption was going to reconnect to, so the mechanism built to survive a roll was undone by its own
cleanup. Every roll cost a sandbox and, with it, the conversation's context.

Hence `keep_sandbox`, which is the one bit of state the whole finalizer branches on: false for an
ending session (closed, or failed in a way the CLI cannot be asked to continue past), true when it
is only this replica that is going away. The `except* WebSocketDisconnect` and
`except CancelledError` paths set it, and both hand the lease back instead of ending the session.

Related but separate: what makes the runner _retry_ after that hand-back is
`BridgeAuthentication.HELD` answering 503, which is
<2026_08_13_sessions_boot_and_die.md> § What the first real roll found.

## The finalizer that stopped at its first await

The `finally` in `handle_runner` is several `await`s in a row, and it commonly runs on a task that
is already cancelled — a rolling replica, an evicted pod. On a cancelled task the first `await`
re-raises `CancelledError` immediately, so the statements after it never ran at all: the socket
was closed, and `client.aclose()`, the claim cleanup and `closed()` were silently skipped. The
session stayed looking alive in the row until the sweep reached it.

`asyncio.shield` around one coroutine that does the whole sequence is what makes the rest of it
reachable. The 10-second `wait_for` bounds it, because a shielded block on the way down is still
inside uvicorn's graceful-shutdown budget.

It is best effort even so, and the comment says so, because this is the trap the fix invites: a
SIGKILL runs no finalizer at all. What actually guarantees a session stops looking alive is the
lease — see <2026_08_13_sessions_boot_and_die.md> § The re-test, which is the run that established
that the finalizer cannot be the thing correctness rests on.

## The sandbox deadline that did not move

A SandboxClaim carries a `shutdownTime`, and it was first set once at creation, to
`now + session_ttl_seconds`. That makes the sandbox's lifetime a hard timer started when the
session was provisioned rather than a statement about whether anyone is using it — so a
conversation in full flow was killed mid-answer at its deadline, having been in constant use for
the whole window.

`_renew_lease` now slides both deadlines on the same heartbeat: the Console lease in Postgres and
the claim's `shutdownTime` through `sandbox_claims.renew`. The property this buys is the one worth
preserving — Console lease and sandbox deadline lapse **together**, the moment a replica stops
tending the session, so there is no state where one of the two believes the session is alive and
the other does not.

## Two facts under one flag

`_run_turn` tracks two things that look like one: whether the room has been queued a reply
(`spoke`, now `session_turns.queued_reply`) and whether any assistant message completed at all
(`saw_assistant_message`, now `said_anything`). They were the same variable.

They come apart on a session with no room. The SPA reads the message rows directly, so nothing is
queued for it, so `spoke` stayed false for a turn that had in fact said everything it had to
say — and the tail of `_run_turn`, seeing `not spoke`, minted a second message row out of
`result.result`, which is a verbatim repeat of the last assistant message. Every SPA answer was
stored twice.

So they are two columns on `session_turns` and two locals here, and the reason each exists is the
other one's blind spot: `queued_reply` is about a debt to a channel, `said_anything` is about the
transcript. The one remaining consumer of the difference is the abort notice, which rides on
`final_text` and therefore on no message row.

## Small ones

- **`SpaSession.surface_column` / `MatrixSession.surface_column`.** The `surface` enum and the
  `room_id` were mapped separately at `create`'s one call site, by an `isinstance` chain that had
  to be right about both. Carrying the column value on the variant is what makes a third surface
  one dataclass rather than two arms to remember.
- **The session view's `responding`.** Derived from the open turn, not read off the column — the
  same single-fact rule as the abort above. The column is still written back in `end_turn` when it
  carries the old meaning, which only a replica on the previous image would have put there; that
  is roll compatibility, tombstoned in <../../plans/chat_runtime_cleanup.md> rather than here.
- **`update_assistant` and the per-delta session write.** It set `status = RESPONDING` on every
  stream delta, which is one session-row write per token batch to hold true a flag the open turn
  already stated.
- **`_projected`'s freshness.** One frame at a time, a new projection each time, because a
  projector held across the turn would merge the frames sharing a `message.id` into a single row
  and defer every completion by one frame. Both are improvements; both change what is stored, so
  neither belongs to a change that stores nothing new (<../../plans/chat_runtime_projection.md>
  § stage 4).
