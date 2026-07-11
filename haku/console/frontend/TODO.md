# haku/console/frontend TODO

- Render datetimes and durations more concisely yet human-readably across the previews.
  Today it's scattered: `shortDate` (approval_state.ts) dumps a full locale date+time, and
  `formatEventDateTime` (tool_previews/google_calendar.tsx) formats all-day dates but still
  echoes the raw ISO string for timed events. Want relative/short forms ("in 2h",
  "tomorrow 9am", "Jul 12") with the absolute value on hover, shared by every widget and
  both preview variants, rather than each field spelling its own format.
