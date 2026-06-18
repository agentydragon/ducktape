# calendar_prep (example)

Read Calendar with the read-only Google token (see `../instructions.md` → _Hard rules_)
over the next ~7–14 days:
`curl -s -H "Authorization: Bearer $TOK" 'https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin=<now>&timeMax=<+14d>&singleEvents=true&orderBy=startTime'`.
Look for:

- events missing prep, an agenda, or travel/buffer time
- conflicts / double-bookings
- meetings implying a task (book travel, prepare a doc, bring something)

File one item per finding, referencing the event by title + start time.
