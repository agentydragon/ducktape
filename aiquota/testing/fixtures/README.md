# Render fixtures

`observed_account_2026_11_04.yaml` comes from the configured AIQuota API's
`/v1/quotas` response, observed on November 4, 2026 at 16:56 Pacific
(`2026-11-05T00:56:28Z`). It retains each provider's `last_output`, before display
filtering. Organization and grant identifiers and names are replaced; replacement
names have the observed lengths. Amounts, minor-unit precision, nulls, list order,
cardinality, window state, product scopes and timestamps are retained.

The snapshot includes Claude prepaid money and a nearly exhausted weekly window,
Codex paid credits and a lightly used weekly window, 105 OpenAI API grants (three
active), and an Anthropic organization with an ordinary grant and a promo grant
whose scope enumerates six broad products. Keep the depleted grants in the input:
the production view model filters them.

Every surface derives its view at the fixture's own `fetched_at`; a shared January
clock would change the November snapshot's expiry, countdown and freshness behavior.
GNOME renders at 1280 by 1024 so its four-provider popup can be reviewed together;
browser captures also include the observed account at a narrow viewport.
