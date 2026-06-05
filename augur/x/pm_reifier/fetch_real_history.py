"""Fetch real recent macro series from public sources -> real_history.json for the windowed spike.

Sources (all public, keyless):
  sp500            <- Yahoo Finance ^GSPC monthly close
  crypto:BTC       <- Yahoo Finance BTC-USD monthly close
  inflation        <- FRED CPIAUCSL (CPI all-urban)                     [normalized to 100 at now]
  home_value:sf_ca <- FRED SFXRSA (Case-Shiller SF home-price index)    [normalized to 100 at now]
  rent:sf_ca       <- FRED CUUR0000SEHA (CPI rent of primary residence) [normalized; NATIONAL proxy]

sp500 and crypto:BTC keep absolute levels; the index series are normalized so the last value = 100.0
(augur's "index = 100 at month 0" convention) while preserving the recent shape. Writes the last 12
monthly points per series. Run: python3 augur/x/pm_reifier/fetch_real_history.py
"""

from __future__ import annotations

import datetime
import json
import pathlib
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).parent
N = 60  # months of recent history (~5 years of context for the model to infer drift/volatility)

FRED = {"inflation": "CPIAUCSL", "home_value:sf_ca": "SFXRSA", "rent:sf_ca": "CUUR0000SEHA"}
YAHOO = {"sp500": "%5EGSPC", "crypto:BTC": "BTC-USD"}
NORMALIZE = {"inflation", "home_value:sf_ca", "rent:sf_ca"}  # rebased to 100 at the last point


def _get(url: str, ua: str, tries: int = 5) -> bytes:
    # Yahoo needs a browser UA; FRED rejects "Mozilla/*" (anti-scraping) but serves a curl-style UA.
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    delay = 2.0
    for i in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


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
        if wire in NORMALIZE:
            series[wire] = [round(v / vals[-1] * 100.0, 2) for v in vals]
        else:
            series[wire] = [round(v, 2) for v in vals]
    out = {
        "as_of": max(months.values()),
        "months_per_series": {w: months[w] for w in series},
        "series": series,
        "raw_last_level": raw_last,
        "sources": {**{w: f"yahoo:{s}" for w, s in YAHOO.items()}, **{w: f"fred:{s}" for w, s in FRED.items()}},
    }
    (HERE / "real_history.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote real_history.json (as_of {out['as_of']})")
    for w, vals in series.items():
        print(f"  {w:18} last={raw_last[w]:>10} ({months[w]})  tail={vals}")


if __name__ == "__main__":
    main()
