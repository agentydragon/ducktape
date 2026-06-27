# Calendar

The operator's schedule — upcoming commitments, availability, and the recurring shape of
their week; a window into what they're doing and when. Read it with the read-only Google
token `$TOK` ([README](README.md)), e.g. the next ~7–14 days:
`curl -s -H "Authorization: Bearer $TOK" 'https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin=<now>&timeMax=<+14d>&singleEvents=true&orderBy=startTime'`.

Each event carries title, start/end, attendees, location, and an `htmlLink` (use it to link
the event in items). Calendar is most useful **cross-referenced** — with mail/Tana to tell
whether an event still stands or implies work, and as the "when/where" that reprioritizes
other items.

What to _do_ with it (prep gaps, conflicts, implied tasks) → the **calendar-prep** pass in
your procedures (`procedures/calendar_and_geo.md`, in your state).
