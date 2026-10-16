# Chart.js regression tests

Line overflow is computed before dataset drawing options are installed. Reading the
previous line element's options there makes layout depend on update history. Keep
first render, repeated update, and resize round-trip coverage when changing the patch
in `//patches:chartjs_line_overflow.patch`; checking only the final render misses it.
