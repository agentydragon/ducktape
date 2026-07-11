# haku/console/frontend TODO

- Render datetimes and durations more concisely yet human-readably across the previews.
  Today it's scattered and literal: `shortDate` (approval_state.ts) dumps a full
  locale date+time, `formatEventDateTime` (tool_previews/google.tsx) echoes the raw ISO
  string, and reminder timings go through `formatDuration`. Want relative/short forms
  ("in 2h", "tomorrow 9am", "Jul 12") with the absolute value on hover, shared by every
  widget and both preview variants, rather than each field spelling its own format.
