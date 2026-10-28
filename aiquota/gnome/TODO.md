# TODO

See `DESIGN.md` for the UX design and rationale; this file lists deferred work.

## v2: burn rate over a rolling history window

The cumulative pace metric (used now) answers "will I make it to reset?". A
separate "burn rate over the last 30 minutes" metric would answer "am I
overspending right now?", which is the question worth asking before kicking
off a heavy task. Requires the extension to keep a small ring buffer of
`(timestamp, used_percent)` samples and finite-difference them. Surface as an
extra popup line: `burn (30m): 4%/h → exhausts in 5h12m at this rate`.

## Option E (logo-as-pie / water fill)

`DESIGN.md` lists this as the cutest panel rendering but the most expensive
to implement (Cairo path-clipped fill on a non-convex SVG mark, illegible
under ~25% / over ~75% fill). Skip unless someone specifically wants it.

## Sparkline of historical pace in the popup

Once the rolling history exists for v2 burn rate, render a tiny sparkline of
"used % over the window so far" beside each row in the popup. Implementation:
`St.DrawingArea` plus Cairo, ~60×16px.
