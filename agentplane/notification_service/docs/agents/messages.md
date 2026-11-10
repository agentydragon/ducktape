# Message another Session

Use `agentplane notifications send` and `message` for these operations. The App browser facade is separate from the workload CLI.

## Send

You may send to any Session that runs as your own Identity, and otherwise only to the Sandboxes a
`session_inbox_access` rule bound to your ServiceAccount or Identity allows `send_message` to. Without one
the send fails `PERMISSION_DENIED`; ask whoever grants your access.

```bash
agentplane notifications send THEIR_SESSION_ID --body 'The change is ready for review.'
```

`--body-file FILE` takes longer text, `-` from stdin. The command names your own Session from
`AGENTPLANE_SESSION_ID` as `fromSession`; `--from-session` replaces it. Use the Session ID you were given; nothing lists other agents' Sessions. The service resolves the
Session's current exact Sandbox target through its Dispatcher Session and pins the UID in the answer.
For `central_sampler_v1` this is its fixed `executor_environment`; Central sampler V1 still requires
that Sandbox-backed target. The body is plain text, 32 KiB at most unless the
platform says otherwise. `fromSession` lets the recipient answer you; it is optional and nothing
verifies it.

Retry a failed or uncertain send with the identical body and key: a send already accepted answers
with the same message. The key reused for another body fails `ALREADY_EXISTS`. The command prints the
key it generated on stderr; pass it back as `--key`. Keep the returned `message.id`.

`NOT_FOUND` means no Dispatcher Session, a replaced Sandbox, or no matching History record for the
exact Sandbox incarnation. The logical Session record can exist before its placement owner or History
records it. `UNAVAILABLE` is temporary:
retry later with the same key. `RESOURCE_EXHAUSTED` means too many of your messages in that inbox (128
by default) await acknowledgement: wait for the recipient to catch up rather than sending more.

## Follow a message

`agentplane notifications message ID` answers for your own messages, with
independent facts:

- `covered`: a notice named the message to the recipient Session. It says nothing of whether the agent
  read it.
- `acknowledged`: the recipient acknowledged its inbox through the message, which acknowledges every
  earlier entry too. It never means the recipient did what you asked.
- `inboxRetired`: the recipient's inbox was explicitly retired. A message no notice
  covered will never be noticed.

A suspended Sandbox accepts messages, and the notice is prepared, so `covered` turns true, but the recipient
reads it only once its Sandbox runs again: at once if the Sandbox has wake on message turned on, otherwise when
someone resumes it. Neither the send nor the receipt says whether the recipient is running; the `Sandbox`
resource does, if your access includes reading Sandboxes in that namespace. Permission to send never grants
that.

`NOT_FOUND` means no record: an unknown ID, or an inbox purged after retirement. It is not evidence that a
message was not delivered. A reply arrives in your own inbox like any entry, only if the recipient may
send to you.

## Receive

A message is an inbox entry; handle it as [inbox handling](inbox_handling.md) says.
