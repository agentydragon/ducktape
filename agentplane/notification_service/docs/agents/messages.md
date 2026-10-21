# Message another Session

Routes and schemas are in [OpenAPI](/openapi.json); the command has no message commands yet.

## Send

You may send only to the Sandboxes a `session_inbox_access` rule bound to your ServiceAccount allows
`send_message` to. Without one the send fails `PERMISSION_DENIED`; ask whoever grants your access.
`POST /v1/messages`:

```json
{
  "destinationRef": { "namespace": "THEIR_NAMESPACE", "name": "THEIR_SANDBOX" },
  "sessionId": "THEIR_SESSION_ID",
  "idempotencyKey": "review-request-1",
  "body": "The change is ready for review.",
  "fromSession": "YOUR_SESSION_ID"
}
```

Use the Sandbox and Session you were given; nothing lists other agents' Sessions. Add `uid` to
`destinationRef` to reach only that incarnation; without it the message goes to the Sandbox of that
name now, and the answer carries its UID. The body is plain text, 32 KiB at most unless the platform
says otherwise. `fromSession` lets the recipient answer you; it is optional and nothing verifies it.

Retry a failed or uncertain send with the identical body and key: a send already accepted answers
with the same message. The key reused for another body fails `ALREADY_EXISTS`. Keep the returned
`message.id`.

`NOT_FOUND` means no such Sandbox, another incarnation than the `uid` given, or a Session the platform
has no record of in that Sandbox; a Session is recorded once it has run. `UNAVAILABLE` is temporary:
retry later with the same key. `RESOURCE_EXHAUSTED` means too many of your messages in that inbox (128
by default) await acknowledgement: wait for the recipient to catch up rather than sending more.

## Follow a message

`GET /v1/messages/{id}` answers for your own messages, with independent facts:

- `covered`: a notice named the message to the recipient Session. It says nothing of whether the agent
  read it.
- `acknowledged`: the recipient acknowledged its inbox through the message, which acknowledges every
  earlier entry too. It never means the recipient did what you asked.
- `inboxRetired`: the recipient's inbox was retired, as when its Sandbox was deleted. A message no notice
  covered will never be noticed.

`NOT_FOUND` means no record: an unknown ID, or an inbox purged after retirement. It is not evidence that a
message was not delivered. A reply arrives in your own inbox like any entry, only if the recipient may
send to you.

## Receive

A message is an inbox entry; handle it as [inbox handling](inbox_handling.md) says.
