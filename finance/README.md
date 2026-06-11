# finance

Finance tools and portfolio tracking utilities.

## Components

- **Worthy**: Rust-based portfolio tracker (uses Cargo/Bazel)
- **Reconciliation utilities**: Various financial system reconciliation tools
- **`evidence/`**: shared read floor for the augur-evidence repo — source specs,
  checkout, loaders, and prediction-market record models (consumed by augur and loom)
- **`scraper/`**: the augur-evidence git scraper (FRED/Yahoo/Zillow sources + the
  prediction-market mirror), deployed as the `augur-evidence` CronJob image
