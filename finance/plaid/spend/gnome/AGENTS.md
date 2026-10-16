GNOME screenshot tests set `PLAID_SPEND_TEST_NOW` before starting GNOME; the
override applies only to the extension's relative sync-age label when
`PLAID_SPEND_TEST=1`. Do not freeze the GLib or desktop clock, which changes
unrelated rendering and behavior.
