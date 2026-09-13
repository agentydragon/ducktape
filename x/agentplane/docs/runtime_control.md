# Runtime control

`ChangeModel` is a durable runner command. `CommandReceived` says only that the runner stored it;
`ModelChanged` says the harness has actually taken effect. There is no public timing selector and
no generic active-turn conditional. The runner calls the harness adapter and waits for its
harness-native evidence.

## Harness evidence

- Claude receives a native `control_request` with subtype `set_model`. Its adapter sends that
  request and emits `ModelChanged` only after the successful native control response. Claude
  accepts this control while a turn is active; the pinned harness test proves the next model
  request uses the selection.
- Codex app-server has no equivalent mutation request. Its adapter retains the requested model
  until the next native `turn/start` response proves that `turn/start` selected it. A later pending
  request supersedes an earlier one, which ends as `CommandNoop`; a native refusal ends both the
  selected model command and input command as `CommandRejected`.

The pinned native surface and mock-LLM tests are summarized in the
[protocol roster](../native/docs/protocol_roster.md). Reasoning effort is not a runtime command:
Claude presently exposes it at launch and Codex has separate thread-config and per-turn fields.
It needs its own evidence-backed command before it joins this protocol.
