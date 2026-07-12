@README.md

## Editing console visuals — always screenshot and eyeball

When you change anything visual in this SPA — the past-tool-calls history view
(`tool_calls_page.tsx`), the console panel/drawer (`console_panel.tsx`), the panel toggle,
the tool-call cards, or their styles (`styles.src.css`) — regenerate and **look at** the
screenshots before handing off:

```bash
bbr test //haku/console/frontend:screenshots
```

It renders each surface (`screenshots/harness.tsx`) in both light and dark themes to PNGs (two
per scene: `history` — its first rows expanded into their detailed state with the Metadata
disclosure open, the rest left compact — `chrome` — the shell chrome with its
approvals/settings/location/live panels stacked — plus `settings` and `previews`) in the test's
**undeclared outputs**.
Browser rendering runs on the RBE worker, so this is a `bbr` test, not a local `bb run`. Fetch
the PNGs
with the `buildbuddy_api` skill (download the target's undeclared test outputs), then open every
light and dark image and actually check it looks good — spacing, alignment, contrast, truncation, that nothing
overflows or collides, and that both content and chrome read clearly — not merely that it
rendered. The test is a generator, not a pixel-diff gate: it passes as long as every scene
renders, so it never blocks CI on "looks different", but a blank/crashed scene fails it.

The `previews` scene is a gallery of **every implemented tool-call preview**, each rendered in
both compact and detailed — when you add or change a per-server widget (`tool_previews/*.tsx`),
add its (server, tool, sample args) to `PREVIEW_SAMPLES` in `screenshots/sample_data.ts` and
re-check the gallery. Add a whole new scene to `screenshots/harness.tsx` (and the `SCENES` list
in `screenshots/render.mjs`) whenever you add a new surface.

## Tool-call rendering — design requirements

A tool call shows up in two places — the approval **drawer** cards (`console_panel.tsx`) and
the **history** rows (`tool_calls_page.tsx`) — but both render through one shared component,
`tool_call_card.tsx` (identity header + action line + status badge + `Details` toggle, the
arguments body, and the detailed Result/Metadata); only the status badge and footer actions
differ per surface. Each renders at one of two variants, **compact** or **detailed**, flipped
per item by its toggle. The argument body is drawn by a per-server **preview widget**
(`tool_previews/`), or the generic raw-JSON fallback (`tool_arguments_field.tsx`). Hold all of
it to these rules so the whole surface reads as one thing.

**Compact is for skimming.** A compact rendering answers "what is this, and do I need to look
closer?" at a glance — nothing more. Show the action, its primary target(s), and just enough
payload to recognize the call: the first few list items then `… +N more`, the first lines of a
body, the couple of fields that matter. Omit provenance, secondary attributes, and raw JSON. If
a compact card makes you expand it in the _common_ case it's showing too little; if it wraps
well past a few lines it's showing too much.

**Detailed is complete, but ranked.** Detailed shows every argument, ordered by importance,
with the rarely-useful parts folded away:

- The raw JSON is always available behind a **`Raw arguments`** disclosure — never inline once
  a widget rendered.
- Provenance that isn't about _what the call does_ — the **caller principal**, the **exact
  timestamp**, the **tool-call id** — goes behind one **`Metadata`** disclosure, not inline.
- What the call does and why — arguments, rationale, denial reason, result — stays visible.

**Don't waste vertical space.**

- Lead with the call's own identity as a heading (event summary, draft subject, delete target),
  not a labelled field.
- Use inline icon fields (`Field icon={…}`) for short labelled values; never a stacked
  uppercase label + line break for a two-word value.
- Collapse pairs and ranges onto one line (a start–end becomes one range; add/remove labels sit
  together).
- One idea per line.

**Consistent vocabulary.**

- The **action** is a one-line description on the card's identity line (`tool_action_line.tsx`),
  not a badge in the body: a registered tool supplies its own via `definePreview`'s third arg
  (`"Gmail: Draft email"`, `"Grocy: Add 5 items to stock"`; destructive ones flagged red), and a
  tool with no widget falls back to `serverId.toolName`. The widget body must not restate it.
- **Identity / target** is bold; secondary attributes are dimmed inline text or small outline
  badges.
- **Icons** replace labels only where the glyph is unambiguous (🕐 time, 📍 place, 👥 people);
  otherwise a short inline label.
- **Semantic color is not the accent** — reserve red for genuinely destructive or failed states.

**Datetimes and durations** use the shared concise forms — `formatTimestamp` (relative when
near, full value on hover) for a wall-clock instant, `formatEventDateTimeRange` for a calendar
start–end — rather than each field spelling its own format.

When you add or change any rendering, put it (both variants) in the `previews` gallery and check
it against this list.
