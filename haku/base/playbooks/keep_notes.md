# keep_notes (example)

Scan Google Keep for captured-but-unhandled notes with the read-only Google
token (see `../instructions.md` → _Hard rules_; same `$TOK`). List notes:
`curl -s -H "Authorization: Bearer $TOK" 'https://keep.googleapis.com/v1/notes'`,
and focus on what's new or changed since your bookmark. Look for:

- to-dos / reminders jotted down but never acted on
- notes that imply a task (a thing to buy, book, follow up on, or research)
- checklists left half-done
- stale notes worth resolving or archiving

File one item per finding, referencing the note by title (or a short quote) +
id. Needs the `keep.readonly` scope on the token, and the Keep API is
Workspace-gated — if the call returns 403 or the API isn't available for this
account, note the gap in your log and skip; don't treat it as an error.
