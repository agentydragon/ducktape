# drive_activity (example)

Scan recent Google Drive activity with the read-only Google token (see
`../AGENTS.md` → _Hard rules_; same `$TOK`). List what changed since your
bookmark, e.g. files by recent modification:
`curl -s -H "Authorization: Bearer $TOK" 'https://www.googleapis.com/drive/v3/files?orderBy=modifiedTime desc&fields=files(id,name,modifiedTime,owners,shared,webViewLink)&pageSize=50'`,
or the Drive Activity API
(`https://driveactivity.googleapis.com/v2/activity:query`) for a change feed.
Look for:

- docs shared with the operator that look like they await a read or reply
- files implying a task (a draft to finish, a form/agreement to sign, a doc to
  review before a meeting — cross-reference `calendar_prep`)
- comments or @-mentions directed at the operator
- stale shared drafts worth closing out

File one item per finding, referencing the file by name + `webViewLink` + last
modified. Needs a Drive read scope on the token (`drive.readonly` /
`drive.metadata.readonly`, and `drive.activity.readonly` for the activity feed);
if a call returns 403, the scope isn't granted — note the gap in your log and
move on.
