# TODO

See `DESIGN.md` for the UX design and rationale; this file lists deferred work.

## v0 follow-ups (settings)

The v0 implementation hard-codes the following constants in `extension.js`. Move
them to a `Gio.Settings` schema (and a preferences UI) once the design has
shaken out:

- `POLL_INTERVAL_SECONDS`
- `STALE_AFTER_SECONDS`
- `PACE_COOL_BELOW`, `PACE_WARN_ABOVE`, `PACE_HOT_ABOVE`
- `SHORT_WIN_HOT_PERCENT`
- `STABLE_FRACTION`
- Toggles: `show-pace-numeral`, `show-percent`, `show-short-window-bar`

## Pace label currently tracks the long window only

`_renderProvider` shows `formatPace(longPace)` next to the icon. The icon's
_tint_ already follows the binding window (short overrides long when hot), but
the numeral always shows the long window's deviation. When the short window is
the binding one, the numeral and the tint can disagree (icon red, numeral
shows a small long-window deviation). Fix: track which window's tint won and
show that window's pace numeral.

## v2: burn rate over a rolling history window

The cumulative pace metric (used now) answers "will I make it to reset?". A
separate "burn rate over the last 30 minutes" metric would answer "am I
overspending right now?", which is the question worth asking before kicking
off a heavy task. Requires the extension to keep a small ring buffer of
`(timestamp, used_percent)` samples and finite-difference them. Surface as an
extra popup line: `burn (30m): 4%/h → exhausts in 5h12m at this rate`.

## Per-model breakdown for Claude

The Claude usage API also returns `seven_day_opus` and `seven_day_sonnet`.
Hide behind `show-model-breakdown` (default off) so the popup doesn't get
crowded for users who don't care.

## Option E (logo-as-pie / water fill)

`DESIGN.md` lists this as the cutest panel rendering but the most expensive
to implement (Cairo path-clipped fill on a non-convex SVG mark, illegible
under ~25% / over ~75% fill). Skip unless someone specifically wants it.

## Sparkline of historical pace in the popup

Once the rolling history exists for v2 burn rate, render a tiny sparkline of
"used % over the window so far" beside each row in the popup. Implementation:
`St.DrawingArea` plus Cairo, ~60×16px.
