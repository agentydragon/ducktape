# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

The runtime and the channels have graduated: the durable record to <../conversation/>, the
runner-incarnation machinery to <../session/>, the wake wires to <../notifications/>, and the
channels to <../channels/> (#4772/#4924; target layout: <../docs/naming_and_layout.md> § 2).
What remains is what is still being restructured:

| Where                                   | What it is                                                                                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conversation_events.py`                | The provider-neutral vocabulary a conversation is read as; deletion-scheduled with the #4667 native-projector fold.                                                       |
| `session_events.py`                     | The stream's two categories as stored rows — the one place the vocabularies meet the table. Merges into `conversation/conversation_event.py` (#4772 C5).                  |
| `runtime.py`, `runtime_catalog.py`      | Backend-neutral runtime catalog and its application composition; the harness _selection_ residue headed for `harnesses/` (<../docs/naming_and_layout.md> § 2).            |
| `x/claude_code/`, `x/codex_app_server/` | **One CLI harness each.** Named for the product whose binary they launch, not for the model. The native client + projection move runner-ward (#4667, deletion-scheduled). |
| `testing/`                              | Stand-ins a test stands something up _with_, importable by non-pytest processes too (<testing/recording_claims.py> for the rationale).                                    |
| `conftest.py`                           | Fixture re-registrations for the tests below this directory; the definitions live in <../session/conftest.py>.                                                            |

How to place a module — the replace-the-other-axis test and the boundary cases — is
<../docs/chat_layers.md> § Placing something new.

## Harness adapters

`claude_code/` speaks Claude Code's newline-delimited JSON: `frames.py` (the CLI's own `type`
vocabulary and the readers that pick a value out of one), `client.py` (the protocol client),
`projection.py` (the reducer into the neutral vocabulary — its contract is its own docstring;
read <../../cli_protocol/protocol.md> and the adjacent fixtures before changing it),
`runtime.py` (the adapter), `wake.py` (classifying idle-time frames), and `redaction.py` with
`frame_export.py`/`frame_export_main.py` — recording a production session as a redacted JSONL
fixture, how-to in <claude_code/frame_export_main.py>.

`codex_app_server/` is the second harness, over Codex's app-server JSON-RPC: the same
client/frames/projection/runtime split, plus `capture.py` for recording sanitized fixtures off
a real Codex.

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
