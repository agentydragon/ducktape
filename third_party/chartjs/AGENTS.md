# Chart.js regression tests

Line overflow is computed before dataset drawing options are installed. Reading the
previous line element's options or stale resolver cache there makes layout depend on
update history. Refresh controller configuration before layout; retain the later refresh
for `beforeDatasetsUpdate` plugins. Keep first render, repeated update, border-change,
and resize round-trip coverage when changing the patch
in `//patches:chartjs_line_overflow.patch`; checking only the final render misses it.
