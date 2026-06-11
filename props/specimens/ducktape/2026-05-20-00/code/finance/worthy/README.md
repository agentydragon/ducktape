# worthy

![](stonks.png)

Personal net worth tracker in Rust. Aggregates assets from multiple sources, converts to a common currency, and runs FIRE (financial independence / early retirement) modeling.

Configuration lives in `~/.config/worthy/config.yaml`.

## Running

```bash
# Snapshot (default): fetch assets + exchange rates, save JSON, print FIRE model
bazel run //finance/worthy:rust_main

# Remodel last snapshot without network calls
bazel run //finance/worthy:rust_main -- --command=modellastsnapshot

# Export all historical snapshots to CSV
bazel run //finance/worthy:rust_main -- --command=csv
```

Enable trace logging with `RUST_LOG=rust_main=trace`.

## Sources

_Sources_ provide current asset holdings:

- **Hardcoded** — manual entries in the config file
- **IBFlex** — Interactive Brokers Flex Query API (long equity positions + cash)

Removed: Coinbase (legacy API deprecated), FTX (exchange collapsed).

## Converters

_Converters_ provide exchange rates to convert everything into `common_currency`:

- **Alpha Vantage** — stock prices and FX rates
- **CurrencyLayer** — foreign exchange rates
- **Fixer.io** — foreign exchange rates

Removed: Coinbase converter.

## Configuration

```yaml
sources:
  bank1:
    name: "Bank 1"
    type: hardcoded
    assets:
      - currency: USD
        amount: 12345.67
  employee_stonks:
    name: "Employee stonks"
    type: hardcoded
    assets:
      - stock: GOOG
        amount: 37.047
  interactive_brokers:
    name: "My Interactive Brokers account"
    type: ibflex
    query_id: your_flex_query_id
    token: your_flex_web_service_token

converters:
  currency_layer:
    type: currencylayer
    api_key: currencylayer_api_key
  alpha_vantage:
    type: alphavantage
    api_key: alphavantage_api_key
  fixer:
    type: fixer
    api_key: fixer_api_key

common_currency: GEL
dated_json_output: "~/worthy-snapshots/%s.json"
csv_output: "~/dropbox/finance/worthy.csv"

modelling:
  monthly_saving:
    currency: CHF
    amount: 10
  yearly_yields: [0.03, 0.06]
  monthly_targets:
    - currency: CZK
      amount: 10000
    - currency: USD
      amount: 100
```

## Interactive Brokers Flex Query setup

1. Log in to the IB portal (<https://ndcdyn.interactivebrokers.com/sso/Login>).
2. Create a Flex query:
   - Top menu -> "Performance & Reports" -> "Flex Queries"
   - Add a new "Activity Flex Query"
   - Select fields in: Account Information, Cash Report, Open Positions, Net Stock Position Summary
   - Copy the query ID -> use as `query_id` in config
3. Enable the Flex web service ([IB docs](https://guides.interactivebrokers.com/am/am/reports/flex_web_service_version_3.htm)):
   - Account settings -> "Account Reporting" -> "Flex Web Service" -> enable and save
   - Copy the generated token -> use as `token` in config

## Useful snippets

Print total from each snapshot:

```bash
for f in *json; do
  echo -n $f ' '
  printf "%d\n" $(jq '.["Total"]["Amount"]' $f)
done
```
