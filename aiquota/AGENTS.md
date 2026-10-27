# aiquota — agent notes

Three surfaces render one data model (<models.py>):

- **CLI** `aiquota` (<cli.py>, <render/human.py>)
- **GNOME extension** (<gnome/extension.js>)
- **Browser dashboard** (<frontend/>, served by the API)

The Haku Console quota panel renders `//aiquota/frontend:board` from its proxy's payload; keep
it that way, never a hand-written copy (the last one drifted).

## Surfaces in lockstep

Any presentation change to one surface is mirrored in the other two (the user reads them side
by side and reports drift as a bug): the single "currently over plan" / "extra spend active"
signal, window-row layout (label, used %, reset, pace, forecast), header (provider,
error/stale annotations, extra-spend tail), collapsed view while burning extra. Shared
formatters go in <render/format.py>, ported to JS.

Both JS surfaces compute live countdown and pace locally because they tick every second. They
share the runtime-neutral implementation in <shared/presentation.ts>; the browser adapts its
snake_case API data in <frontend/format.ts>, and the GNOME zip carries the emitted module
beside `extension.js`. The browser format test replays Python-generated cases, and the GNOME
render test loads the packaged extension across the shared scenarios.

Scenarios in `testing/fixtures/*.yaml` feed all three: CLI snapshots (`render/test_human.py`),
extension renders (`gnome/test_render.py`), dashboard screenshots in both themes
(`frontend:screenshots`).

**The board must survive embedding**: `frontend/board.css` is `aiquota-`-prefixed with the
palette scoped to `.aiquota-scope`; the component neither fetches nor reads a clock (host
passes `now`). No bare element selectors, `:root` tokens or `useEffect` fetches.

## Provider API quirks

- Claude OAuth usage and z.ai monitor APIs return camelCase: models need
  `alias_generator=to_camel`, else fields silently parse as `None` (CLI shows "no credentials
  found" / "0m to renew"). New providers: same config, plus a test round-tripping a realistic
  response.
- "Currently over plan" = any window ≥100% usage. `ExtraSpend.is_enabled` only means the
  feature is on; `used_usd` is month-cumulative. Surfaces consume this via
  `currently_over_plan` / `extra_status` in <render/view_model.py>.
