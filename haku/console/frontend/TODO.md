# haku/console/frontend TODO

- Add previews for the remaining **`tana`** tools: `list_workspaces`, `search_nodes`, `read_node`,
  `get_children`, `list_tags`, `get_tag_schema`, `tag`, `set_field_content`, `create_tag`,
  `add_field_to_tag`, `set_tag_checkbox`, `check_node`, `uncheck_node`, and `open_node`.
  The generic JSON preview remains intentional until each has a useful operator-focused rendering.
- Cover `startGeolocationWatch` and `stopGeolocationWatch` in `bridge.test.ts`. `parseInbound`
  handles both, but the spec exercises only `requestGeolocation`, and nothing forces the parser to
  learn a new `Inbound` variant. Consider inferring the parser from a shared runtime schema, or an
  exhaustive per-variant test.
- Check the Calendar preview's date handling against the tool's date-time contract.
  `parseEventDate` in `tool_rendering/google_calendar/requests.tsx` reads `date_time` as the event's
  own wall clock and keeps its first five time characters, so an offset instant such as
  `2026-09-15T16:00:00Z` with `time_zone: America/Los_Angeles` would render 16:00.
