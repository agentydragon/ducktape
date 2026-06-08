"""Fetch recent monthly macro history from public sources (Yahoo Finance + FRED).

Live-refresh companion to the checked-in snapshots in this directory (see SOURCES.md). All sources
are public and keyless:

  sp500            <- Yahoo Finance ^GSPC monthly close
  crypto:BTC       <- Yahoo Finance BTC-USD monthly close
  inflation        <- FRED CPIAUCSL (CPI all-urban)                     [normalized to 100 at the last point]
  home_value:sf_ca <- FRED SFXRSA (Case-Shiller SF home-price index)    [normalized to 100 at the last point]
  rent:sf_ca       <- FRED CUUR0000SEHA (CPI rent of primary residence) [normalized; NATIONAL proxy]

`sp500` and `crypto:BTC` keep absolute levels; the index series are normalized so the last value =
100.0 (augur's "index = 100 at month 0" convention) while preserving the recent shape. `main()` writes
the last N monthly points per series to `real_history.json`.

Run: bb run //finance/augur/data:fetch_real_history
"""

from __future__ import annotations

import datetime
import json
import pathlib
import urllib.error
import urllib.request

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

HERE = pathlib.Path(__file__).parent
N = 60  # months of recent history (~5 years)

FRED = {"inflation": "CPIAUCSL", "home_value:sf_ca": "SFXRSA", "rent:sf_ca": "CUUR0000SEHA"}
YAHOO = {"sp500": "%5EGSPC", "crypto:BTC": "BTC-USD"}
NORMALIZE = {"inflation", "home_value:sf_ca", "rent:sf_ca"}  # rebased to 100 at the last point


def _is_transient(exc: BaseException) -> bool:
    """A network read worth retrying: a 429/5xx response, or a transport-level timeout/URL error."""
    if isinstance(exc, urllib.error.HTTPError):  # HTTPError is a URLError subclass — check it first
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, TimeoutError | urllib.error.URLError)


@retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(url: str, ua: str) -> bytes:
    # Yahoo needs a browser UA; FRED rejects "Mozilla/*" (anti-scraping) but serves a curl-style UA.
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return bytes(resp.read())


def yahoo_monthly(sym: str) -> list[tuple[str, float]]:
    d = json.loads(
        _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1mo&range=10y", ua="Mozilla/5.0")
    )
    r = d["chart"]["result"][0]
    by_month: dict[str, float] = {}
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"], strict=False):
        if c is not None:
            by_month[datetime.datetime.fromtimestamp(t, datetime.UTC).strftime("%Y-%m")] = c
    return sorted(by_month.items())


def fred_monthly(series_id: str) -> list[tuple[str, float]]:
    txt = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", ua="curl/8.0").decode()
    out: list[tuple[str, float]] = []
    for line in txt.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in (".", ""):
            out.append((parts[0][:7], float(parts[1])))
    return out


def main() -> None:
    series: dict[str, list[float]] = {}
    raw_last: dict[str, float] = {}
    months: dict[str, str] = {}
    for wire, sym in YAHOO.items():
        pts = yahoo_monthly(sym)[-N:]
        series[wire] = [round(v, 1) for _, v in pts]
        raw_last[wire] = round(pts[-1][1], 1)
        months[wire] = pts[-1][0]
    for wire, sid in FRED.items():
        pts = fred_monthly(sid)[-N:]
        vals = [v for _, v in pts]
        raw_last[wire] = round(vals[-1], 3)
        months[wire] = pts[-1][0]
        series[wire] = (
            [round(v / vals[-1] * 100.0, 2) for v in vals] if wire in NORMALIZE else [round(v, 2) for v in vals]
        )
    out = {
        "as_of": max(months.values()),
        "months_per_series": months,
        "series": series,
        "raw_last_level": raw_last,
        "sources": {**{w: f"yahoo:{s}" for w, s in YAHOO.items()}, **{w: f"fred:{s}" for w, s in FRED.items()}},
    }
    (HERE / "real_history.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote real_history.json (as_of {out['as_of']})")
    for wire, vals in series.items():
        print(f"  {wire:18} last={raw_last[wire]:>10} ({months[wire]})  tail={vals}")


if __name__ == "__main__":
    main()
