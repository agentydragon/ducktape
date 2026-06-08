# Public exogenous evidence sources

Snapshots of publicly available exogenous data that augur's models fit
against. None of this is private; every series is reproducible by following
the steps below. Two files have been trimmed from their upstream form to
keep the checked-in size manageable; the trim is also documented per file.

To refresh any series, replace the file in place using the steps below.

## Files

### FRED series

[FRED](https://fred.stlouisfed.org/) is the St. Louis Fed's economic data
service. For any series `<SERIES_ID>`, the CSV download is:

```
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
```

(Or equivalently, browse to `https://fred.stlouisfed.org/series/<SERIES_ID>`
and click the **Download → CSV** button.)

The files checked in are **untrimmed** — column shape and date range match
upstream as of the last refresh. Series mapping:

| Local file                          | FRED series ID   | What it is                                                                                         |
| ----------------------------------- | ---------------- | -------------------------------------------------------------------------------------------------- |
| `fred_cpi_us.csv`                   | `CPIAUCSL`       | US headline CPI (all items, urban consumers, seasonally adjusted)                                  |
| `fred_sp500.csv`                    | `SP500`          | S&P 500 daily close                                                                                |
| `fred_mortgage30.csv`               | `MORTGAGE30US`   | 30-year fixed mortgage rate (Freddie Mac PMMS, weekly)                                             |
| `fred_sfxrsa.csv`                   | `SFXRSA`         | Case-Shiller SF home price index, seasonally adjusted                                              |
| `fred_fhfa_sf_oakland_berkeley.csv` | `ATNHPIUS41884Q` | FHFA SF-Oakland-Berkeley MSA all-transactions HPI (quarterly)                                      |
| `fred_sf_rent_cpi.csv`              | `CUURA422SEHA`   | SF-area rent CPI — only the FRED-only degraded evidence path; production rent is Zillow ZORI below |

**Known gap — October 2025 CPI.** BLS published **no October-2025 CPI**: the
government shutdown disrupted collection that month, so BLS skipped the standalone
October release. Every CPI product is affected — `CPIAUCSL` and the `CUUR*` rent
series (`CUURA422SEHA`, `CUUR0000SEHA`) have **no `2025-10` row at all** (the CSV
jumps `2025-09 → 2025-11`); non-CPI series (Yahoo, Case-Shiller `SFXRSA`) are
unaffected. This is source-side and permanent — re-downloading does not recover
it. Consumers that align series month-by-month see a one-month hole here; do not
mistake it for a fetch bug or forward-fill an invented CPI value.

### `yahoo_spy_chart_adjusted.json`, `yahoo_btc_chart_adjusted.json`, `yahoo_eth_chart_adjusted.json`

Three Yahoo Finance v8 chart-API snapshots, all trimmed to the same minimal
shape (`meta.symbol`, `meta.currency`, `timestamp`, `indicators.adjclose[0].adjclose`)
so the same loader path (`_read_yahoo_spy_adjusted_close`) reads them all.

| Local file                      | Symbol    | What it is                                                                                                                      |
| ------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `yahoo_spy_chart_adjusted.json` | `SPY`     | State Street SPY ETF daily prices — SP500 total-return proxy (captures dividend reinvestment, which FRED `SP500` does not).     |
| `yahoo_btc_chart_adjusted.json` | `BTC-USD` | Bitcoin price. Yahoo aggregates crypto at coarser-than-daily under `range=max`; loader's `_monthly_last` normalizes either way. |
| `yahoo_eth_chart_adjusted.json` | `ETH-USD` | Ethereum price (same caveat as BTC).                                                                                            |

Source: Yahoo Finance v8 chart API. Substitute `<SYMBOL>` per row above:

```
curl -sS 'https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>?range=max&interval=1d' \
  -H 'User-Agent: Mozilla/5.0' -o yahoo_<symbol>_chart_adjusted.json
```

**Trimmed**. The upstream response carries six daily series under
`chart.result[0].indicators.quote[0]` (open/high/low/close/volume + redundant
adjclose) plus the OHLC/volume bundle, market-meta blocks, and trading-hours
windows. The loader (`augur/fit/evidence_data.py::_read_yahoo_spy_adjusted_close`)
only reads `chart.result[0].timestamp` and
`chart.result[0].indicators.adjclose[0].adjclose`. The checked-in file
preserves only those two arrays plus a minimal `meta.symbol` /
`meta.currency` for traceability. Size: ~1 MB upstream → ~240 KB trimmed.

After a fresh download, re-trim with:

```python
import json, sys
data = json.load(open(sys.argv[1]))
result = data['chart']['result'][0]
trimmed = {
    'chart': {
        'result': [{
            'meta': {'symbol': result['meta']['symbol'], 'currency': result['meta'].get('currency')},
            'timestamp': result['timestamp'],
            'indicators': {'adjclose': [{'adjclose': result['indicators']['adjclose'][0]['adjclose']}]},
        }]
    }
}
json.dump(trimmed, open(sys.argv[2], 'w'), separators=(',', ':'))
```

### `zillow_city_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv`

Zillow's city-level Home Value Index for the mid-tier SFR + condo bucket,
smoothed and seasonally adjusted, monthly. Used as the home-price ground
truth for SF and Vallejo location paths.

Source: [Zillow Research](https://www.zillow.com/research/data/) — pick
**Home values → ZHVI All Homes (SFR, Condo/Co-op) Time Series, Smoothed,
Seasonally Adjusted ($) → City**.

Direct URL (subject to change as Zillow rotates dataset paths):

```
https://files.zillowstatic.com/research/public_csvs/zhvi/City_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv
```

**Trimmed**. The upstream file is the full nationwide CSV with ~21,000 city
rows × ~25 years of monthly columns (~88 MB). The loader
(`augur/fit/evidence_data.py::_zillow_city_series`) filters by
`RegionType == "city" && State == "CA" && RegionName ∈ {San Francisco,
Vallejo}`. The checked-in file preserves only those two rows plus the header.
Size: ~88 MB upstream → ~15 KB trimmed.

To re-trim after a fresh download:

```python
import csv, sys
with open(sys.argv[1], newline='') as fin, open(sys.argv[2], 'w', newline='') as fout:
    reader, writer = csv.reader(fin), csv.writer(fout)
    writer.writerow(next(reader))
    for row in reader:
        if row[3] == 'city' and row[5] == 'CA' and row[2] in ('San Francisco', 'Vallejo'):
            writer.writerow(row)
```

Add the corresponding `(RegionName, State)` pair to the filter when extending
the location set.

### `zillow_city_zori_uc_sfrcondomfr_sm_sa_month.csv`

Zillow's city-level Observed Rent Index (ZORI) for the SFR + condo/co-op + MFR
bucket, smoothed and seasonally adjusted, monthly. The production rent ground
truth for SF and Vallejo location paths — same Zillow methodology as the ZHVI
home-value file, so the SF/Vallejo rent cross-covariance is consistent. (Mare
Island has no separate rent index; the model mirrors `rent:vallejo_ca` for it.)

Source: [Zillow Research](https://www.zillow.com/research/data/) — pick
**Rentals → ZORI (Smoothed, Seasonally Adjusted): All Homes Plus Multifamily
Time Series ($) → City**.

Direct URL (subject to change as Zillow rotates dataset paths):

```
https://files.zillowstatic.com/research/public_csvs/zori/City_zori_uc_sfrcondomfr_sm_sa_month.csv
```

**Trimmed** with the same `(RegionType, State, RegionName)` filter as the ZHVI
file above (the loader reuses `_zillow_city_series`); the checked-in file keeps
only the San Francisco + Vallejo CA city rows plus the header. Size: ~4.5 MB
upstream → ~5 KB trimmed.

## Prior and reference sources

The `state_space` trainer persists a `prior_manifest` in each trained artifact.
The manifest records the configured prior choices and links them to the evidence
that produced the artifact. Public reference sources to use when revisiting
those priors:

| Source                                              | URL                                                                                                         | Use                                                                 |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| FRED CPIAUCSL                                       | https://fred.stlouisfed.org/series/CPIAUCSL                                                                 | CPI level history and inflation sanity checks.                      |
| Philadelphia Fed Survey of Professional Forecasters | https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/survey-of-professional-forecasters | External inflation forecast priors and posterior predictive checks. |
| Kenneth French Data Library                         | https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html                                   | Equity factor and market-risk reference data.                       |
| Damodaran industry betas                            | https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html                                     | Weak external anchor for private-company equity beta priors.        |
| World Bank GDP                                      | https://data.worldbank.org/indicator/NY.GDP.MKTP.CD                                                         | World-GDP macro-capacity reference for private-equity scale priors. |
| Stan prior choice recommendations                   | https://github.com/stan-dev/stan/wiki/prior-choice-recommendations                                          | General weakly-informative prior guidance.                          |
| NumPyro distributions documentation                 | https://num.pyro.ai/en/stable/distributions.html                                                            | Distribution/covariance primitives used by Augur fit code.          |

Large or mutable raw public files should either be normalized into small,
reviewable checked-in snapshots or pinned as Bazel `http_file` repositories with
`sha256` and a comment explaining the source. Do not make training depend on a
mutable live URL without a checksum-pinned snapshot or mirror. CRSP is explicitly
not a public download source; use it only if a licensed access path is configured
outside this repository.

Private company observations and their source notes live in the private
deployment repository; do not copy personal or company-specific facts into
this public data directory.

## Refresh checklist

When refreshing one or more series:

1. Re-download from the source.
2. Apply the trim (Yahoo, Zillow) if applicable.
3. Replace the file in place. Don't rename — the filenames are referenced as
   path constants in `augur/fit/evidence_data.py`.
4. Re-fit downstream models that depend on the changed series:
   the active trained VECM provider, plus any downstream trained blobs stored
   outside this package.
