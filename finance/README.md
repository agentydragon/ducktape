# finance

Finance tools and portfolio tracking utilities.

## Components

- **Worthy**: Rust-based portfolio tracker (uses Cargo/Bazel)
- **`plaid/spend/`**: statement-cycle card spend API, terminal CLI and GNOME client
- **`evidence/`**: shared read floor for the augur-evidence repo — source specs,
  checkout, and market snapshot loaders used by augur. The Manifold bet history
  and current comments remain mirrored for future loom market harvesting; no
  current loader reconstructs historical prices from them.
- **`scraper/`**: the augur-evidence git scraper (FRED/Yahoo/Zillow sources + the
  prediction-market mirror). Its cluster pipeline is parked; the retained market roster
  for a future revival lives in the [retained market roster](scraper/market-roster.yaml).
- **`budget/`**: Plaid transaction classifier (bucket taxonomy, rule DSL, SQL read
  model) behind augur's Budget tab and the Beancount exporter

## Plans

- [FIRE dashboard](plans/fire_dashboard.md): a household page that shows spending read
  fresh from the accounts beside the distribution of financial-independence outcomes
  under named economy models.
