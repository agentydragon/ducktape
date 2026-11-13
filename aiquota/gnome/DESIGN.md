# GNOME presentation design

The panel is a compact cue; the popup carries the details needed to plan usage.
The panel shows provider marks with a short pace or status label. It renders the
shared view model's selected reading and tint instead of deriving quota policy
in the shell. API billing providers stay in the popup.

The popup shows each displayed quota window with its usage, reset state, and a
meter whose fill is quota used and whose tick is elapsed time. The forecast
helps distinguish a window likely to exhaust early from one with quota left at
reset. The panel keeps the summary readable while the popup exposes those
comparisons.

The rolling burn-rate and sparkline ideas remain deferred in [TODO.md](TODO.md);
current pace is cumulative from the start of each quota window. Runtime
rendering lives in [extension.js](extension.js) and the [shared view model](../render/view_model.py),
with the appearance covered by [GNOME render tests](test_render.py).

Provider SVG marks in `icons/` are third-party assets and are not covered by the
repository's AGPL license; they are included for personal use only.
