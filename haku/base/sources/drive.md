# Google Drive

Recent Google Drive activity is a **window into what the operator is currently
working on** — use it both to orient (what's going on this week, what they care
about right now) and to spot concrete ways to help. Read it with the read-only
Google token (the read-only Google token `$TOK`, see [README](README.md)): list what
changed since your bookmark, e.g. files by recent modification
`curl -s -H "Authorization: Bearer $TOK" 'https://www.googleapis.com/drive/v3/files?orderBy=modifiedTime desc&fields=files(id,name,modifiedTime,owners,shared,webViewLink)&pageSize=50'`,
or the Drive Activity API
(`https://driveactivity.googleapis.com/v2/activity:query`) for a change feed.

Let it inform your wider reasoning — a burst of edits on a doc or project tells
you what to cross-reference elsewhere (calendar, mail) and where help is welcome
— and look for direct findings:

- docs shared with the operator that look like they await a read or reply
- files implying a task (a draft to finish, a form/agreement to sign, a doc to
  review before a meeting — cross-reference the calendar channel)
- comments or @-mentions directed at the operator
- a project they're actively editing where you could offer to draft, summarize,
  research, or prepare the next step
- stale shared drafts worth closing out

File one item per finding, referencing the file by name + `webViewLink` + last
modified. Needs a Drive read scope on the token (`drive.readonly` /
`drive.metadata.readonly`, and `drive.activity.readonly` for the activity feed);
if a call returns 403, the scope isn't granted — note the gap in your log and
move on.
