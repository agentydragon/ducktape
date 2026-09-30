# Why the session watch answers 404

`GET /v1/code/sessions/watch` answers 404 to the sync's OAuth bearer on `api.anthropic.com`, for a resume token the
list route had just issued ([docs/api.md](../docs/api.md) § Session watch). The event stream and the list route work
with the same token. Delete this note once the cause is known and `docs/api.md` states it.

## Candidates

1. **A gate on the account.** The web client's own code uses the watch only while the server-side gate
   `amber_harbor_beacon` is on, and polls the list otherwise. The server may 404 a gated route.
2. **The first-party host does not serve the route.** It is the web client's route, same-origin on `claude.ai`.
3. **A header the sync does not send.** The web client also sends `anthropic-client-feature: ccr` and
   `anthropic-client-platform: web_claude_ai`.

## Run the probe

The probe is read-only and never refreshes the token, so it is safe beside the running sync. It reads the access token
from a copy of the credential file (the running sync refreshes the original, so take a fresh copy if it says the token
has lapsed):

```bash
kubectl -n claude-session-sync exec deploy/claude-session-sync -- cat /data/credentials.json > /tmp/claude-credential.json
bb run //devinfra/claude/session_export:export_sessions_bin -- probe --credentials-file /tmp/claude-credential.json
rm /tmp/claude-credential.json
```

Add `--session session_<id>` while a session is running to also hold its event stream open and see which frames arrive.
Or run it inside the pod, where the file is already in place:

```bash
kubectl -n claude-session-sync exec deploy/claude-session-sync -- \
  /devinfra/claude/session_export/export_sessions_bin probe --credentials-file /data/credentials.json
```

It prints one line per variant: the status, what was asked, and the body or the shape of the frames that arrived (JSON
keys, never values).

## Reading the result

- **`watch with no resume_token` answers 400 while the others answer 404:** the route exists and is refusing this
  request or account. Whichever variant turns 404 into 200 names the missing header. If none does, suspect the gate.
- **A header variant answers 200:** send that header from `watch_sessions`.
- **`watch on claude.ai` answers 200 or 401 rather than 404:** the route lives on `claude.ai`; 200 means the bearer works
  there, 401 or 403 means it needs the cookie session.
- **Everything answers 404, including `/v1/sessions/watch` and `claude.ai`:** the route is not offered to this
  credential; discovery by listing stays the mechanism.
- **The event stream lines** show whether `client_event` frames arrive while a session runs.
