# Codex transcript record-shape audit

This bounded audit informed the Codex transcript adapters in `frontend/transcript-rows.ts` and
`frontend/event-content.tsx`. It is evidence from three explicitly selected local rollouts, not a
claim about all Codex archives. The inventory sampled the first 7,245 lines of the active parent
rollout and the full two comparison files; it recorded field names and type counts only, not
transcript text.

| Sample | Lines | Bytes |
| --- | ---: | ---: |
| `~/.codex/sessions/2026/10/30/rollout-2026-10-30T22-19-55-01a19066-7395-7380-87e7-d3d7368c2b26.jsonl` (bounded prefix) | 7,245 | 32,422,746 |
| `~/.codex/sessions/2026/10/30/rollout-2026-10-30T23-58-57-01a190c1-1e0e-7b00-82d8-b0cf1b707ae2.jsonl` | 461 | 2,204,460 |
| `~/.codex/sessions/2026/10/28/rollout-2026-10-28T23-53-57-01a1866f-d0d8-72d2-a156-2413f6173515.jsonl` | 29 | 207,659 |
| **Total inspected** | **7,735** | **34,834,865** |

The selected response payloads contained 874 `reasoning`, 321 `message`, 117 `agent_message`,
131 `function_call`, 131 `function_call_output`, 732 `custom_tool_call`, and 731
`custom_tool_call_output` records. Message bodies were arrays of `input_text` (1,738 blocks) and
`output_text` (210). Agent-message content used `input_text` (117 blocks), with
`encrypted_content` present in 96 records. All 874 sampled reasoning summaries were empty arrays
and had a string `encrypted_content`; this bounded absence does not invalidate the typed
`summary_text[]` shape.

Tool call arguments and custom-tool inputs were strings (131 and 732 records). Of 862 tool
outputs, 730 custom-tool outputs and 4 function outputs were arrays; those array elements were
typed `input_text` (1,739 blocks) or `input_image` (92 blocks). The remaining 127 function outputs
and 1 custom-tool output were strings. The sampled outputs did not use `content`, `stdout`,
`stderr`, `error`, `is_error`, or `name` as output aliases. A few sampled `response_item` records
had type `compaction` and encrypted content; the separate top-level `compacted` records carried a
`message` plus compaction metadata, not a `summary` field.

The local Codex request model at `agentplane/native/codex/testing/requests.py` independently
defines message content as a string or `input_text`/`output_text` blocks; agent content as
`input_text`/`encrypted_content`; function-call arguments and custom-tool input as strings; tool
output as a string or `input_text`/`input_image` blocks; and reasoning summary as
`summary_text[]`. The frontend keeps those typed fields, preserves `input_image` output blocks for
inspection, and leaves unmatched shapes on their source records instead of treating similarly
named fields as prose or failure markers.

One correlated `/root/disclosures` to `/root` handoff was traced across the sender's
`collaboration.send_message` argument and the receiver's `agent_message.content`:
`input_text` held the routing header and `encrypted_content` held the exact matching encrypted
body. The sender tool result was empty, and neither sender call/result nor the correlated receiver
record contained plaintext. This establishes that storage path for this handoff only; it says
nothing about plaintext copies in other messages or unrelated transcript text. No decryption key
was sought.
