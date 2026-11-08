# aiquota — agent notes

Three surfaces render one data model (<models.py>):

- **CLI** `aiquota` (<cli.py>, <render/human.py>)
- **GNOME extension** (<gnome/extension.js>)
- **Browser dashboard** (<frontend/>, served by the API)

## Surfaces in lockstep

Any presentation change to one surface is mirrored in the other two (the user reads them side
by side and reports drift as a bug): the single "currently over plan" / "extra spend active"
signal, window rows (label, used %, reset, forecast; numeric pace in details), labelled balance and
grant rows, and explicit stale/error/reset-pending states. Keep window rows present in paid mode.
The tmux segment is intentionally a concise summary using the selected window and status,
with API billing providers omitted. The GNOME panel likewise keeps API billing in the popup. Shared
quota presentation decisions live in <render/view_model.py>. Its display selection identifies
the current or last substantive result and its timestamp; clients render that selection and
the latest fetch error without choosing a fallback themselves. Prepaid balance can have a
separate stale timestamp because it is fetched independently. `balances` and `grants` normalize
provider-specific billing fields while retaining organizations, units, grant restrictions, and
separate pools. Dates use `format_deadline` / `formatDeadline`; never add local date formats.
`stale_at` is the server-owned freshness deadline; disconnected clients can compare their
clock to it without inventing an age threshold. A passed reset deadline means awaiting a
new reading, never a refilled bucket.

GNOME and web consume <shared/presentation.ts> for dates, money, reset timing and freshness
deadlines; keep these helpers free of React and GNOME imports.

The API emits time-refreshed window presentation views on the event stream. Python computes
pace, forecast, tint and meter position once in the shared view model; the CLI, browser and
GNOME extension render those fields. Presentation updates do not poll providers. The desktop
daemon forwards the server view and only writes its raw cache when the quota data changes.

Scenarios in `testing/fixtures/*.yaml` are `AllQuotas` Pydantic model data with fixed timestamps.
CLI snapshots (`render/test_human.py`) load those models directly; GNOME and browser fixtures
are generated through `to_view(...).model_dump(mode="json")`, the same snake_case view contract
served by `/v1/quotas` and the desktop daemon. GNOME passes that payload through `_loadViewData`.
The scenarios feed GNOME renders (`gnome/test_render.py`) and dashboard screenshots in both
themes (`frontend:screenshots`).

**The board must survive embedding**: `frontend/board.css` is `aiquota-`-prefixed with the
palette scoped to `.aiquota-scope` and derived from the host Mantine theme; the host supplies
`MantineProvider` and Mantine styles. The component neither fetches nor reads a clock (host passes
`now`). No bare element selectors, `:root` tokens or `useEffect` fetches.

## Provider API quirks

- Claude OAuth usage and z.ai monitor APIs return camelCase: models need
  `alias_generator=to_camel`, else fields silently parse as `None` (CLI shows "no credentials
  found" / "0m to renew"). New providers: same config, plus a test round-tripping a realistic
  response.
- "Currently over plan" = an exhausted window with extra usage enabled; it does not prove
  requests are happening or a charge was incurred. `ExtraSpend.is_enabled` only means the
  feature is on; `used_usd` is month-cumulative. Surfaces consume this via
  `currently_over_plan` / `extra_status` in <render/view_model.py>.
