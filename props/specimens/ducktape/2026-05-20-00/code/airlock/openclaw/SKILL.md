# Airlock

You have access to privileged tools which require user approval. Access to the
MCP servers providing these tools is gated by an airlock MCP server.

The exec environment provides `AIRLOCK_URL` and `OPENCLAW_SESSION_ID`
automatically. You can interact with the Airlock using any MCP client;
`mcporter` is one such tool available in the exec environment.

## Discovering available tools

Read the server's instructions and list available tools before submitting
actions:

```bash
mcporter instructions "$AIRLOCK_URL"
mcporter list-tools "$AIRLOCK_URL"
```

Follow the instructions when submitting actions. Use `list-tools` to see
tool names, descriptions, and input schemas.

## Requesting an action

```bash
mcporter call "$AIRLOCK_URL" <tool-name> '{"arg": "value", "session_key": "'$OPENCLAW_SESSION_ID'"}'
```

You will receive an immediate acknowledgment with an action key
(`session_key`/`action_seq`). The action is then queued for user review.

## Receiving results

When the user approves or denies an action, you will receive a system
notification with the result. You do not need to poll — results are delivered
automatically.

## Checking action state

Read the state of a specific action:

```bash
mcporter read "$AIRLOCK_URL" "resource://sessions/$OPENCLAW_SESSION_ID/actions/<action_seq>"
```

Browse the session event log:

```bash
mcporter read "$AIRLOCK_URL" "resource://sessions/$OPENCLAW_SESSION_ID/log_hwm"
mcporter read "$AIRLOCK_URL" "resource://sessions/$OPENCLAW_SESSION_ID/log/<entry_id>"
```

## Withdrawing a pending action

If you no longer need a pending action, you can withdraw it:

```bash
mcporter call "$AIRLOCK_URL" withdraw '{"session_key": "'$OPENCLAW_SESSION_ID'", "action_seq": <seq>}'
```
