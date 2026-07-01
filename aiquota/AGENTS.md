# aiquota — agent notes

AI subscription quota tracker. Two surfaces share the same data model and
rendering rules:

- **CLI** — `aiquota` (Python, `cli.py` + `render/human.py`)
- **GNOME Shell extension** — `aiquota/gnome/extension.js` panel button + popup

## Keep the CLI and GNOME extension in lockstep

The CLI default output and the GNOME popup are two presentations of the same
state. **Any presentation change to one must be mirrored in the other.** That
includes:

- What counts as "currently over plan" / "extra spend active" (single signal —
  do not let the two surfaces drift apart on this).
- Window-row layout (label, used %, reset, pace, forecast).
- Header structure (provider name, error/stale annotations, extra-spend tail).
- Collapsed-view behavior when the user is currently burning extra.

If you change one and not the other, the user will report it as a bug. The
data model lives in `aiquota/models.py`; shared formatters belong in
`aiquota/render/format.py` (and should be ported to the JS side when added).

## Provider API quirks

Both the Claude OAuth usage API and the z.ai monitor API return camelCase
JSON. Pydantic models therefore set `alias_generator=to_camel` — without it
the snake_case fields silently parse as `None` and the CLI degrades to
"error — no credentials found" / `reset_seconds=0` ("0m to renew"). When
adding a new provider, default to the same alias config and write a test
that round-trips a realistic API response.

Claude's `spend.enabled`, surfaced internally as `ExtraSpend.is_enabled`,
only signals "feature enabled on this account", not "currently paying above
subscription". The "currently over plan" signal is any window at or above
100% usage. `ExtraSpend.used_usd` is cumulative across the billing month,
not a right-now indicator. Both surfaces consume this rule via the shared
Python view model (`currently_over_plan` / `extra_status` in
`render/view_model.py`).
