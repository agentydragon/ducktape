GNOME screenshot tests set `PLAID_SPEND_TEST_NOW` before starting GNOME; the
override applies only to relative server-sync and desktop attempt/retry labels
when `PLAID_SPEND_TEST=1`. The test interface may advance that label clock to
verify countdown updates. Do not freeze the GLib or desktop clock, which changes
unrelated rendering and behavior.
