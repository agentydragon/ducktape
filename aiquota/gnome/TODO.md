# TODO

See `DESIGN.md` for the UX design and rationale; this file lists deferred work.

## Rolling burn rate

The cumulative pace metric (used now) answers "will I make it to reset?". A
separate "burn rate over the last 30 minutes" metric would answer "am I
overspending right now?", which is the question worth asking before kicking
off a heavy task. Requires the extension to keep a small ring buffer of
`(timestamp, used_percent)` samples and finite-difference them. Surface as an
extra popup line: `burn (30m): 4%/h → exhausts in 5h12m at this rate`.

## Sparkline of historical pace in the popup

After the rolling history exists, show the "used % over the window so far"
history beside each row in the popup.
