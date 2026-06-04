"""LLM-as-base-measure spike for the prediction-market reifier (augur/x, throwaway).

Question: can an LLM act as the base measure Q in the "reify PM marginals into trajectories"
plan — i.e. emit a diverse cloud of typed numeric scenarios whose marginals, after one
max-ent reweight to the market prices, snap to those prices without the effective sample
size collapsing? See augur/plans/interpolating_prediction_markets.md.

Pure stdlib (no numpy on the host). Every request/response is written to transcripts/.
Run: python3 augur/x/pm_reifier/run_spike.py
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).parent
TRANSCRIPTS = HERE / "transcripts"
RESULTS = HERE / "results"
# Key from $ZAI_API_KEY, else /tmp/zai_key (mirrored from the claude-sandbox `zai-api-key` secret).
KEY = (os.environ.get("ZAI_API_KEY") or pathlib.Path("/tmp/zai_key").read_text()).strip()

ENDPOINT = "https://api.z.ai/api/coding/paas/v4/chat/completions"
QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
MODEL = "glm-4.6"
SCENARIOS_PER_CALL = 10
N_CALLS = 8

# Knot years the LLM fills (year-end values); "today" is 2026-06 = month index 0.
YEARS = ["2026", "2027", "2028", "2029", "2030", "2031", "2032"]
ANCHORS = "Today is 2026-06. S&P 500 ~ 5300. BTC ~ 95,000 USD. OpenAI last private round ~ $0.85T."

# Illustrative-but-plausible crowd marginals (a coherent monotone ladder per underlying).
# kind: "ge" (series year-end >= threshold) or "ipo_by" (openai ipo_month_index <= month).
MARKETS = [
    {"id": "sp500>6000@2027", "kind": "ge", "series": "sp500_year_end", "year": "2027", "thr": 6000, "price": 0.55},
    {"id": "sp500>8000@2030", "kind": "ge", "series": "sp500_year_end", "year": "2030", "thr": 8000, "price": 0.45},
    {"id": "sp500>10000@2032", "kind": "ge", "series": "sp500_year_end", "year": "2032", "thr": 10000, "price": 0.38},
    {"id": "btc>150k@2027", "kind": "ge", "series": "btc_usd_year_end", "year": "2027", "thr": 150_000, "price": 0.50},
    {"id": "btc>500k@2030", "kind": "ge", "series": "btc_usd_year_end", "year": "2030", "thr": 500_000, "price": 0.30},
    {"id": "openai_ipo<=2027", "kind": "ipo_by", "month": 18, "price": 0.30},
    {"id": "openai_ipo<=2029", "kind": "ipo_by", "month": 42, "price": 0.65},
    # FINDING: despite the "in USD" schema, GLM-4.6 emits OpenAI valuations in TRILLIONS (e.g. 2.8,
    # 12.5 = $2.8T, $12.5T). So ">$1T" is threshold 1.0 here, not 1e12. See README "units" finding.
    {"id": "openai_val>1T@2030", "kind": "ge", "series": "openai_valuation_usd_year_end", "year": "2030", "thr": 1.0, "price": 0.55},
]

SCHEMA = (
    "Each scenario is an object with EXACTLY these keys:\n"
    '  "label": short string,\n'
    '  "sp500_year_end": {"2026":int,...,"2032":int}  (S&P 500 index level at each year end),\n'
    '  "btc_usd_year_end": {"2026":int,...,"2032":int}  (BTC price in USD),\n'
    '  "openai_ipo_month_index": int or null  (months from 2026-06 until OpenAI IPOs; null = no IPO by 2032),\n'
    '  "openai_valuation_usd_year_end": {"2026":number,...,"2032":number}  (OpenAI enterprise value in USD).\n'
    "All seven years 2026..2032 must be present in every year-end map."
)


def _post(body: dict, tag: str) -> dict:
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        err = None
    except urllib.error.HTTPError as e:
        resp, err = {"error": e.read().decode()[:1000], "code": e.code}, e.code
    dt = time.time() - t0
    (TRANSCRIPTS / f"{tag}.json").write_text(json.dumps({"request": body, "response": resp, "latency_s": dt}, indent=2))
    if err is not None:
        raise RuntimeError(f"HTTP {err} for {tag}: {resp}")
    return resp


def quota() -> dict[str, float]:
    req = urllib.request.Request(QUOTA_URL, headers={"Authorization": f"Bearer {KEY}"})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())["data"]["limits"]
    out = {}
    for lim in data:
        if lim.get("type") == "TOKENS_LIMIT" and lim.get("unit") == 3:
            out["short_5h_pct"] = lim.get("percentage")
        if lim.get("type") == "TOKENS_LIMIT" and lim.get("unit") == 6:
            out["weekly_7d_pct"] = lim.get("percentage")
    return out


def market_prompt(conditioned: bool, nonce: int) -> list[dict]:
    sys = (
        "You generate plausible future macro/market SCENARIOS as structured numeric JSON. "
        "Output ONLY a JSON object {\"scenarios\": [ ... ]} and nothing else. "
        f"Produce exactly {SCENARIOS_PER_CALL} DIVERSE scenarios spanning the full plausible range — "
        "optimistic, median, AND pessimistic, explicitly including tail outcomes (market crashes, "
        "AI-driven booms, OpenAI never IPOing, OpenAI IPOing huge). Each scenario must be internally "
        "consistent across series and over time (correlated assets move together; a crash year hits "
        "equities and crypto together; valuations and IPO timing cohere)."
    )
    user = f"{ANCHORS}\n\n{SCHEMA}\n\n(diversity seed {nonce}: make these scenarios different from a typical run.)"
    if conditioned:
        lines = "\n".join(
            f"  - {m['id']}: probability {m['price']:.2f}"
            + (f"  (S&P {m['year']} year-end ≥ {m['thr']:,})" if m["kind"] == "ge" and "sp500" in m["series"] else "")
            for m in MARKETS
        )
        user += (
            "\n\nCrowd prediction-market probabilities to honor IN DISTRIBUTION (the fraction of your "
            f"scenarios satisfying each should roughly match):\n{lines}"
        )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def generate(conditioned: bool) -> tuple[list[dict], int]:
    variant = "conditioned" if conditioned else "unconditioned"
    scenarios: list[dict] = []
    tokens = 0
    for i in range(N_CALLS):
        body = {
            "model": MODEL,
            "temperature": 1.1,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": market_prompt(conditioned, nonce=i),
        }
        resp = _post(body, f"{variant}_call{i:02d}")
        tokens += resp.get("usage", {}).get("total_tokens", 0)
        content = resp["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
            batch = parsed["scenarios"] if isinstance(parsed, dict) else parsed
            scenarios.extend(s for s in batch if isinstance(s, dict))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  [{variant} call{i}] parse failure: {e}")
    return scenarios, tokens


def _get(scn: dict, series: str, year: str) -> float | None:
    try:
        return float(scn[series][year])
    except (KeyError, TypeError, ValueError):
        return None


def indicator(scn: dict, m: dict) -> int | None:
    """1/0 if the scenario satisfies the market; None if the scenario lacks the field."""
    if m["kind"] == "ge":
        v = _get(scn, m["series"], m["year"])
        return None if v is None else int(v >= m["thr"])
    if m["kind"] == "ipo_by":
        if "openai_ipo_month_index" not in scn:
            return None
        ipo = scn["openai_ipo_month_index"]
        return 0 if ipo is None else int(ipo <= m["month"])
    raise ValueError(m["kind"])


def reweight(g: list[list[int]], targets: list[float], ridge: float = 0.05, steps: int = 2000, lr: float = 0.5):
    """Max-ent (min-KL-to-uniform) reweighting: w_i ∝ exp(Σ_m λ_m g[i][m]); fit λ so the reweighted
    marginals match `targets`, with an L2 (ridge) penalty so an infeasible/incoherent target set
    degrades to a soft least-divergence compromise instead of diverging."""
    n, mk = len(g), len(targets)
    lam = [0.0] * mk
    for _ in range(steps):
        logits = [sum(lam[m] * g[i][m] for m in range(mk)) for i in range(n)]
        z = max(logits)
        ws = [math.exp(x - z) for x in logits]
        s = sum(ws)
        w = [x / s for x in ws]
        marg = [sum(w[i] * g[i][m] for i in range(n)) for m in range(mk)]
        lam = [lam[m] + lr * ((targets[m] - marg[m]) - ridge * lam[m]) for m in range(mk)]
    ess = 1.0 / sum(x * x for x in w)
    return w, marg, ess


def evaluate(scenarios: list[dict], variant: str) -> dict:
    valid = []
    for scn in scenarios:
        inds = [indicator(scn, m) for m in MARKETS]
        if all(v is not None for v in inds):
            valid.append([int(v) for v in inds])
    n = len(valid)
    targets = [m["price"] for m in MARKETS]
    raw = [sum(row[m] for row in valid) / n for m in range(len(MARKETS))] if n else []
    w, post, ess = reweight(valid, targets) if n else ([], [], 0.0)
    report = {
        "variant": variant,
        "scenarios_returned": len(scenarios),
        "scenarios_valid": n,
        "ess": ess,
        "ess_frac": ess / n if n else 0.0,
        "markets": [
            {"id": MARKETS[m]["id"], "price": targets[m], "raw": round(raw[m], 3), "reweighted": round(post[m], 3)}
            for m in range(len(MARKETS))
        ]
        if n
        else [],
    }
    print(f"\n=== {variant}: {n}/{len(scenarios)} valid | ESS {ess:.1f} ({report['ess_frac'] * 100:.0f}% of valid) ===")
    print(f"  {'market':22} {'price':>6} {'raw':>6} {'reweighted':>11}")
    for row in report["markets"]:
        print(f"  {row['id']:22} {row['price']:>6.2f} {row['raw']:>6.2f} {row['reweighted']:>11.2f}")
    return report


def main() -> None:
    TRANSCRIPTS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}% short5h={q0.get('short_5h_pct')}%")
    total_tokens = 0
    reports = []
    for conditioned in (False, True):
        variant = "conditioned" if conditioned else "unconditioned"
        print(f"\n--- generating {variant} ({N_CALLS} calls x {SCENARIOS_PER_CALL}) ---")
        scenarios, tokens = generate(conditioned)
        total_tokens += tokens
        (RESULTS / f"{variant}_scenarios.json").write_text(json.dumps(scenarios, indent=2))
        reports.append(evaluate(scenarios, variant))
    q1 = quota()
    summary = {
        "model": MODEL,
        "total_tokens": total_tokens,
        "quota_before": q0,
        "quota_after": q1,
        "weekly_pct_delta": (q1.get("weekly_7d_pct") or 0) - (q0.get("weekly_7d_pct") or 0),
        "reports": reports,
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"\n=== cost: {total_tokens} tokens | weekly quota {q0.get('weekly_7d_pct')}% -> "
        f"{q1.get('weekly_7d_pct')}% (delta {summary['weekly_pct_delta']}pp) ==="
    )


if __name__ == "__main__":
    main()
