# Shared dashboard date presentation

`presentation.js` is the platform-neutral JavaScript module used directly by the Plaid web app and packaged into both GNOME extension ZIPs. `date_format.py` provides matching Python helpers for AIQuota renderers and the Plaid CLI. `date_presentation_vectors.json` is consumed by both runtimes' tests.

Date-only values such as `YYYY-MM-DD` stay calendar dates and are never converted through the viewer's timezone. Instants render in the viewer's timezone with fixed English month and weekday names and a 24-hour clock. The year appears only when the date is outside the viewer's current local year; a clock appears only within fourteen elapsed days. Normal views omit timezone labels.

Each standalone Python app wheel bundles `personal_dashboard` so its own runtime is self-contained. Nix builds AIQuota and Plaid Spend as separate Python applications with isolated site-packages. Installing both app wheels into one shared Python environment would create overlapping ownership of `personal_dashboard`; use the Nix application packages or extract a single shared wheel before combining them in one environment.
