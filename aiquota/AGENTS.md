# aiquota — agent notes

AI subscription quota tracker. Three surfaces share the same data model and
rendering rules:

- **CLI** — `aiquota` (Python, `cli.py` + `render/human.py`)
- **GNOME Shell extension** — `aiquota/gnome/extension.js` panel button + popup
- **Browser dashboard** — `aiquota/frontend/` (React), served by the API

## Keep the surfaces in lockstep

The CLI default output, the GNOME popup and the dashboard are presentations of
the same state, read side by side. **Any presentation change to one must be
mirrored in the others.** That includes:

- What counts as "currently over plan" / "extra spend active" (single signal —
  do not let the surfaces drift apart on this).
- Window-row layout (label, used %, reset, pace, forecast).
- Header structure (provider name, error/stale annotations, extra-spend tail).
- Collapsed-view behavior when the user is currently burning extra.

If you change one and not the others, the user will report it as a bug. The
data model lives in `aiquota/models.py`; shared formatters belong in
`aiquota/render/format.py` (and should be ported to the JS side when added).

Both JS surfaces re-implement the live-countdown formatting and the pace math
(`format.py`, `pace.py`), because those move between snapshots while the popup
and the page tick once a second. The dashboard's port
(`aiquota/frontend/{format,pace}.ts`) is gated: `format.test.ts` replays cases
generated from the Python renderers for the shared scenarios, so editing one
side alone fails. The GNOME copy has no such gate yet — change it and the
Python together by hand.

The three surfaces are reviewed on the same scenarios
(`aiquota/testing/fixtures/*.yaml`): the CLI snapshots them
(`render/test_human.py`), the extension renders them
(`gnome/test_render.py`), and the dashboard renders them in both themes
(`frontend:screenshots`). Add a scenario there and all three pick it up.

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
