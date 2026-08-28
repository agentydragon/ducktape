# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

The runtime and the channels have graduated: the durable record to <../conversation/>, the
runner-incarnation machinery to <../session/>, the wake wires to <../notifications/>, and the
channels to <../channels/> (#4772/#4924; target layout: <../docs/naming_and_layout.md> § 2).
What remains is what is still being restructured:

| Where                                   | What it is                                                                                                                                                             |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conversation_events.py`                | The neutral in-memory event vocabulary log writes are expressed in, and `stored`, its bridge into the durable one (`../conversation/conversation_event.py`).           |
| `runtime.py`, `runtime_catalog.py`      | Backend-neutral runtime catalog and its application composition; the harness _selection_ residue headed for `harnesses/` (<../docs/naming_and_layout.md> § 2).         |
| `x/claude_code/`, `x/codex_app_server/` | **One CLI harness each, launch-only.** Named for the product whose binary they launch, not for the model. The native protocol and projection live runner-ward (#4667). |
| `testing/`                              | Stand-ins a test stands something up _with_, importable by non-pytest processes too (<testing/recording_claims.py> for the rationale).                                 |
| `conftest.py`                           | Fixture re-registrations for the tests below this directory; the definitions live in <../session/conftest.py>.                                                         |

How to place a module — the replace-the-other-axis test and the boundary cases — is
<../docs/chat_layers.md> § Placing something new.

## Harness adapters

`claude_code/` and `codex_app_server/` are launch-only (#4667): each names the CLI whose binary it
launches, and the runner (<../../runtime/x/bridge/>) owns that CLI's native protocol, projection and
fixture capture.

- `claude_code/`: `frames.py` (Claude Code's own `type` vocabulary and the readers that pick a value
  out of one, kept for classifying stored frames) and `runtime.py` (the launch adapter, which builds
  the `HarnessLaunch` the journal bridge sends the runner).
- `codex_app_server/`: `config.py` (the deploy-owned implementation config) and `runtime.py` (the
  Codex launch adapter).

## Tests

The runtime-level fixture definitions live in <../session/conftest.py> (see
<../session/README.md> § Tests run against a real database for the no-fake-stores rule);
<conftest.py> re-registers them for the harness and e2e tests below this directory.

<test_generation_cutover_e2e.py> is the post-cut stack end to end: a real runner process on a
real websocket journaling to a real Console handler — the generation window's health gate. The
channel e2e tiers live with the channel (<../channels/matrix/README.md>).

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_layers.md> — what a session, a conversation and a channel each own, the two
  edges between them, and where a new module, table, port or event kind goes. Read it before
  adding one.
- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../plans/conversation_layers.md> — what is still wrong and the order to fix it.
