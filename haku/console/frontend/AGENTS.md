@README.md

## Visual changes: render and look before hand-off

```bash
bbr test //haku/console/frontend:screenshots [--test_filter=settings]
bbr test //haku/console/frontend/tool_rendering/<server>:previews [--test_filter=<tool name>]
```

- `:screenshots` renders the production shell (`harness/harness.tsx`) in light and dark to
  undeclared outputs: Haku UI (mock iframe) with drawer open/closed, narrow viewport, Settings,
  history, each sync-indicator state. Fetch PNGs with the `buildbuddy_api` skill and inspect every
  image. It fails only on a blank/crashed scene, never on visual change.
- New surface → new scene in `harness/harness.tsx` plus a test in `test_visual.py` that reaches and
  captures it.
- Tool-call preview cards: one `:previews` target per server under `tool_rendering/<server>/`
  (`preview_fixtures.ts` mounted by `preview_harness.tsx`; shared harness in
  `tool_rendering/screenshot/`, `preview_screenshots` macro in `screenshot_test.bzl`), each
  emitting its own `visual-review.json` figure. Adding/changing a widget → add a
  `RegisteredToolPreviewFixture` to that server's `preview_fixtures.ts`, covering both variants.
- A single-component scene renders inside its real production container (preview cards:
  `.haku-shell-panels`) and screenshots that element — no hardcoded width or full-viewport shot
  (<../../../util/testing/frontend_visual/README.md>).

## Tool-call rendering

Approvals-panel cards (`shell_chrome.tsx`) and history rows (`tool_calls_page.tsx`) share
`tool_call_card.tsx`; only status badge and footer actions differ. Each item toggles **compact** /
**detailed**. Arguments: `tool_rendering/<server>/requests.tsx` widget or raw-JSON
`tool_arguments_field.tsx`; results: `responses.tsx` or `tool_result_field.tsx` (raw result is
detailed-only, so compact shows a result only via a widget).

**Combined widget** (`tool_rendering/<server>/calls.tsx`, `defineCallPreview` in
`call_entry.tsx`, dispatched via `toolCallPreview`) only when the result mostly restates the
arguments (e.g. `google_calendar/create_event`). It gets `{ args, result, variant }`, `result`
`undefined` while pending, on failure, or on result-schema mismatch; the card already shows the
error line, so handle pending and finished only. A tool uses exactly one of the two systems.

- **Compact**: action, primary target(s), just enough payload to recognize (first few items then
  `… +N more`, first lines of a body). No provenance, secondary attributes, or raw JSON.
- **Detailed**: every argument, ranked by importance. Raw JSON behind **`Raw arguments`** /
  **`Raw result`** disclosures once a widget rendered; caller principal, exact timestamp, and
  tool-call id behind one **`Metadata`** disclosure; arguments, rationale, denial reason, result
  stay visible.
- **Density**: call identity (event summary, draft subject, delete target) as an unlabelled bold
  heading; short values as inline `Field icon={…}`, never stacked uppercase label + line break;
  pairs/ranges on one line; one idea per line.
- **Typography**: body copy `PreviewText` (incl. `span` fragments; its `sm` default blocks
  Mantine's larger default); identities `PreviewTitle` (weight, not size); supporting text
  `PreviewText` + `c="dimmed"` or the failure color; `size="xs"` only for tertiary UI (`MoreLine`,
  low-priority links, card-level rationale/metadata). Badges `PreviewBadge` (`variant="outline"`
  for attributes, `"light"` for state; size is not hierarchy). Labelled values via `Field` (`mono`
  for opaque ids). Spacing: `Stack gap="xs"` between sections, `2`/`4` within a tight multi-line
  item, `Group gap={6}` inline; no one-off margins or font-size CSS in a server renderer.
- **Reuse** `MoreLine`, `COMPACT_ITEM_LIMIT`, `Field`, date/time formatters, batch/result helpers;
  a pattern used by two servers moves up into `tool_rendering/`.
- **Action** line comes from the React-free `tool_rendering/<server>/actions.ts` map (e.g.
  `"Grocy: Add 5 items to stock"`, destructive flagged red; fallback `serverId.toolName`); the body
  never restates it.
- Secondary attributes: dimmed inline text or small outline badges. Icons replace labels only when
  unambiguous (🕐 time, 📍 place, 👥 people). Red only for destructive/failed.
- Links: `link.tsx`'s `ExternalLink` (new tab, always underlined), never bare Mantine `Anchor`.
- Datetimes: `formatTimestamp` (relative when near, full on hover); calendar ranges
  `formatEventDateTimeRange`.
