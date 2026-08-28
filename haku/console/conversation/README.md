# haku/console/conversation — the durable, provider-neutral record

A conversation is the one thread every surface reads and the only thing a channel or a session
is offered — the layer contract is <../docs/chat_layers.md>, the schema's invariants
<../docs/conversation_schema.md>. Graduated from `../x/` under #4772; the target layout is
<../docs/naming_and_layout.md> § 2 (the one Pydantic event vocabulary, `conversation_event.py`,
lands with C5 and until then stays in `../x/{conversation_events,session_events}.py`).

| Module                | Role                                                                                                                                           |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `log.py`              | The only writer of `conversation_event`/`conversation_item`/`conversation_turn`: the log first, the entities from it, one transaction.         |
| `journal_consumer.py` | Commits the runner's neutral-operation journal into the record: validation, atomic idempotent commit, ACK/resume (#4667).                      |
| `prompt_inbox.py`     | The durable prompt inbox: what the Console has accepted and still owes a runner, pending → withdrawn.                                          |
| `prompt_origin.py`    | Whose voice a prompt is: the origin arms a stored prompt carries.                                                                              |
| `reads.py`            | What a conversation read hands back, and the cursors that page them.                                                                           |
| `item_reads.py`       | The one place a materialised item row folds onto `reads.py`'s entry union — one entry per row, identical for the MCP reader and the SPA views. |
| `reader.py`           | The actor-scoped read surface the `haku_conversations` tools serve.                                                                            |
| `follow.py`           | `WS /api/conversations/{id}/follow`: a conversation's state and the changes to it, as one operation.                                           |
| `live_updates.py`     | Conversation changes as console-socket invalidations for open tabs.                                                                            |
| `history.py`          | The finished conversation tail handed to a replacement session.                                                                                |
| `runtime.py`          | Elected reconciler (`CRUN`): conversation-owned prompt demand into sessions, plus global lease/claim maintenance.                              |
| `reprojection.py`     | Re-project a recorded session's frames and report where the stored log disagrees.                                                              |
