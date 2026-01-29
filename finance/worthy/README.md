# worthy

![](stonks.png)

My net worth tracker in Rust.

`worthy` tracks all your based on a YAML configuration file, prints their value
and logs everything in a JSON file.

Its main file is in `main.go`.

Nobody in their right mind should use random scripts from the internet
for financial stuff. I'm not responsible if it eats your cat... or stonks.

Have fun.

License is GPL 2.

## Sources

`worthy` can get assets in your portfolio from several _sources_:

- Coinbase,
- Interactive Brokers,
- numbers hardcoded in the configuration file (in case
  the institution has no API).

## Converters

Converting various assets into a common currency is handled by getting
current exchange rates from various _converters_:

- Coinbase (for cryptocurrencies),
- Alpha Vantage (for stonks, but can also handle some cryptocurrencies),
- CurrencyLayer (for currencies).

## Dependencies

- Bazel: <https://bazel.build>
- For Rust, `libopenssl-dev` and `pkg-config`.

## Building

```bash
bazel build //finance/worthy:rust_main
```

## Set up hooks

Run this from the repository root:

```bash
cd $(git rev-parse --show-toplevel)
ln -s ../../hooks/pre-commit .git/hooks/pre-commit
```

## Running

With `-command=snapshot` or no value of `-command`, `worthy` takes a snapshot
of current assets in all sources, conversion rates from converters, and saves
it into a configured directory. Then it prints its financial independence model
based on the result.

With `-command=modellastsnapshot`, `worthy` loads the last snapshot and prints
out a financial independence model based on it, without loading any fresh data
from the internet. (It's useful if you don't have internet, or are tinkering
with the modelling algorithm and want to rerun it without the slow network
stuff.)

With `-command=csv`, `worthy` reads all historical snapshots and dumps the
history of the net value of your assets into a CSV file in a predefined
location. You can use `worthy/worthy/plot-net-worth.gnuplot` to plot this data
as a beautiful graph.

## Configuration

Drop a configuration file like this in `~/.config/worthy/config.yaml`:

```yaml
sources:
  bank1:
    name: "Bank 1"
    # If the bank is not integrated in Worthy, hardcode the numbers
    # here and update them manually from time to time.
    type: hardcoded
    assets:
      - currency: USD
        amount: 12345.67
  bank2:
    name: "Bank 2"
    type: hardcoded
    assets:
      - currency: CZK
        amount: 999999
  employee_stonks:
    name: "Employee stonks"
    # You can also hardcode a "source" that contains stonks,
    # not currency.
    type: hardcoded
    assets:
      - stock: GOOG
        amount: 37.047
  interactive_brokers:
    name: "My Interactive Brokers account"
    # Get up-to-date stonks at runtime from Interactive Brokers,
    # instead of hardcoding.
    type: ibdock
    username: your_ib_username
    password: your_ib_password
  coinbase:
    name: "Coinbase"
    # Get up-to-date Coinbase account balances.
    type: coinbase
    # Make sure to use a read-only Coinbase API key!
    api_key: coinbase_api_key
    api_secret: coinbase_secret
converters:
  currency_layer:
    type: currencylayer
    cache_path: "/tmp/currency_layer_cache.json"
    api_key: currencylayer_api_key
  alpha_vantage:
    type: alphavantage
    cache_path: "/tmp/alpha_vantage_cache.json"
    api_key: alphavantage_api_key
  coinbase:
    type: coinbase
    api_key: coinbase_api_key
    api_secret: coinbase_secret

# Your assets will be converted into one common currency for display.
common_currency: GEL

# On each run of worthy in snapshot mode (-command not specified or
# "snapshot"), a JSON file with the current assets in all sources and
# conversions from converters will be dumped here.
dated_json_output: "~/worthy-snapshots/%s.json"

# With -command=csv, worthy will convert the JSON snapshots (see above) into
# a historical CSV that you can plot. It will be saved here.
csv_output: "~/dropbox/finance/worthy.csv"

# Used for FIRE (financial independence/early retirement) modelling.
modelling:
  # Specifies how much you are saving up monthly, and in what currency.
  monthly_saving:
    currency: CHF
    amount: 10
  # Specifies yearly yields to model.
  yearly_yields: [0.03, 0.06]
  # Specifies montly spending targets to model.
  monthly_targets:
    - currency: CZK
      amount: 10000
    - currency: USD
      amount: 100
```

## Interactive Brokers Flex query setup

- Log in into the IB portal (<https://ndcdyn.interactivebrokers.com/sso/Login>).
- Create the Flex query:
  - Top menu -> "Performance & Reports" -> click "Flex Queries"
  - Add a new "Activity Flex Query"
  - Fill in a Query Name (e.g.: "Worthy Flex query")
  - Select all fields in these sections:
    (Not all of those are probably necessary but let's see if it works with them.)
    - Account Information
    - Cash Report
    - Open Positions
    - Net Stock Position Summary
  - Keep all other fields at default values.
  - Click "Continue" -> click "Create"
  - Copy the ID of the newly created Flex query, that'll go to the `query_id` field of
    the `ibflex` source.
- Enable the Flex web service (following <https://guides.interactivebrokers.com/am/am/reports/flex_web_service_version_3.htm>):
  - Go to account settings
    (<https://portal.interactivebrokers.com/AccountManagement/AmAuthentication>)
    -> under "Account Reporting", click "Flex Web Service"
  - Check "Flex Web Service Status", click Save
  - Copy the generated token, that'll go to the `token` field of the `ibflex`
    source.

## Needs

- Make a **read-only** Coinbase API key.

TODO(prvak): It would be nice to also be able to get exchange rates from
Interactive Brokers.

TODO(prvak): If I want to run out of money exactly on day X (e.g., 2090-01-01),
how much longer should I gather money?

TODO(prvak): Store everything in JSON file in deterministic order.

## Contributing

TODO: currently broken

```bash
bazel run //tools:buildifier
```

## Some useful stuff

```bash
for f in *json; do
  echo -n $f ' '
  printf "%d\n" $(jq '.["Total"]["Amount"]' $f)
done
```

```bash
cargo update
cargo raze --generate-lockfile
```
