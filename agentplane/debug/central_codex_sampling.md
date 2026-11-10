# Central Codex subprocess spike

Worker `/root/central_codex_spike`, 2026-10-27. This is the first experimental slice of running
upstream Codex outside its executor, prompted by [incident #12203](https://github.com/agentydragon/ducktape/issues/12203).
The temporary fixture source has been retired; this note preserves its observed behavior and limitations.

The repository pin `rust-v0.162.1` runs both app-server and exec-server. The app-server has its own
`CODEX_HOME` and native frame journal; the executor has a separate workspace and minimal environment.
A scripted Responses server serves the GPT-6 Luna catalog entry under the fixture model slug.
`environment/add` plus `thread/start.environments` select the recording WebSocket relay.
`CODEX_EXEC_SERVER_URL` suppresses the local environment. The relay forwards original WebSocket
messages, records RPCs and never resubmits a tool request.

Observed on RBE:

- Real code-mode shell and `apply_patch` calls travel over executor RPCs. A shell sees an
  executor-only environment marker and no `OPENAI_API_KEY` or sampler-only fake model key.
- Remote workspace `AGENTS.md` reaches the next model request. Code-mode `view_image` returns
  an image that `image(...)` emits into the next Responses request as `input_image`.
- Killing exec-server after observing a shell append leaves `thread/read` available. The
  app-server survives, and a later app-server process reads the retained conversation without
  starting inference. The append is not repeated.

The image/tool/history and explicit-loss-policy test passed in 16.3 seconds, with Ruff and mypy
on the changed libraries:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/3fc68191-0bb3-440a-a67c-0bb98d0c5c3f).
This is whole-test duration, not nested-call latency.

## Loss observation matters

A yielded `exec_command` is not an executor-loss failure receipt. Even after exec-server dies,
Codex can return already-buffered output and a non-null `session_id`. Its native
`commandExecution` item can say `completed` for that yielded call while the process has no observed
exit. A check that merely excludes exit code zero would incorrectly accept this as failure evidence.
The stricter probe observed this in
[invocation d333b719](https://app.buildbuddy.io/invocation/d333b719-3c01-4d63-9190-bdeb402c073a).
The fixture follows the returned session with read-only `write_stdin`, without replaying its shell
text. That poll can also return a live session with empty output after five seconds. Upstream alone
has not supplied a terminal failure at those observation points.

The experimental host policy then checks `environment/status=disconnected`, writes an
`effects=unknown` receipt outside the executor, and requests `turn/interrupt`. Its acceptance is a
native `interrupted` completion and retained history after a clean sampler restart. No second
assistant response is fabricated to turn the loss into apparent success. This is an explicit host
policy, not a claim that upstream automatically fails the turn. The receipt is a fixture file, not
a replicated durable command authority. Exactly-once shell effects remain impossible to infer from
a missing completion receipt: the test author knows the append happened; a surviving sampler
cannot know every effect.

## Open gates

These processes share a host filesystem and Unix principal. Observed executor RPCs and distinct
state directories demonstrate placement and routing; they do not prevent the executor from
accessing the sampler's files. The fixture kills only its exec-server process, and explicitly
cleans up its known blocked shell. It does not prove arbitrary descendant fencing or resilience
to the reported virtiofs daemon failure.

Separate Pod/VM mounts and failure domains, a real executor fence, authenticated Gateway routing
over the runner's outbound stream, executor LLM egress denial, spend attribution and a comparable
latency benchmark remain untested. No staging mode, grant, principal or network route changes.
There is no production central sampler or new conversation authority in this PR.
