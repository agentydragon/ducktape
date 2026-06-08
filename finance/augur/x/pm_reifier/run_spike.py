"""LLM-as-base-measure spike for the prediction-market reifier (augur/x, throwaway).

Question: can an LLM act as the base measure Q in the "reify PM marginals into trajectories"
plan — i.e. emit a diverse cloud of trajectories *in augur's native shape* whose marginals,
after one max-ent reweight to the market prices, snap to those prices without the effective
sample size collapsing? See augur/plans/interpolating_prediction_markets.md.

augur's native trajectory (augur/model/state_space.py): a DENSE MONTHLY level path per factor,
shape (rollout, horizon_months+1, factors). Factors are augur wire-ids: `inflation` (CPI index),
`sp500`, `crypto:BTC`, `home_value:<loc>`, `rent:<loc>`, plus private-equity issuer marks. So the
LLM emits dense monthly paths over exactly those series (no annual knots, no post-hoc
interpolation — month resolution end to end) plus one PE issuer (OpenAI: valuation path + IPO
month). Market thresholds are then evaluated at specific MONTH indices on the dense paths.

Pure stdlib (no numpy on the host). Every request/response is written to transcripts/.
Run: python3 augur/x/pm_reifier/run_spike.py
"""

from __future__ import annotations

import datetime
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

GENERAL = "https://api.z.ai/api/paas/v4/chat/completions"
CODING = "https://api.z.ai/api/coding/paas/v4/chat/completions"
QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

# Model candidates in PREFERENCE order. The paid coding-plan tier (coding endpoint) has dedicated,
# much higher rate limits than the throttled free general tier, and draws the weekly token quota
# (tracked in results/quota_log.jsonl). The free *-flash general models are kept only as a fallback
# for when we want $0 and can tolerate 429s. pick_model() probes and uses the first that answers.
CANDIDATES = [
    (CODING, "glm-4.7"),  # paid coding plan — fast, dedicated rate limits (preferred)
    (CODING, "glm-4.6"),  # paid coding plan — known-good fallback
    (GENERAL, "glm-4.7-flash"),  # free, throttled — fallback only
    (GENERAL, "glm-4.5-flash"),  # free, throttled — fallback only
]

HORIZON_MONTHS = 57  # dense monthly: index 0 = 2026-06 .. index 57 = 2031-03 (just past the furthest market)
LOC = "sf_ca"  # single location for home_value/rent in this prototype
ANCHORS = (
    "Today is 2026-06 (month index 0). Anchor levels at month 0: CPI index 100.0, S&P 500 ~5300, "
    "BTC ~95,000 USD, SF home-price index 100.0, SF market-rent index 100.0, "
    "OpenAI enterprise value ~0.85 (trillions USD)."
)

# augur factor wire-ids -> the dense monthly level path the LLM fills (length HORIZON_MONTHS+1, m0 anchored).
LEVEL_SERIES = {
    "inflation": "CPI index (100.0 at month 0)",
    "sp500": "S&P 500 index level",
    "crypto:BTC": "BTC price in USD",
    f"home_value:{LOC}": "SF home-price index (100.0 at month 0)",
    f"rent:{LOC}": "SF market-rent index (100.0 at month 0)",
}

# Illustrative-but-plausible crowd marginals on the DENSE paths. Month indices: 2027-12=m18,
# 2029-12=m42, 2030-12=m54 (all <= HORIZON_MONTHS).
#   kind "ge_at":   level_series[month] >= thr
#   kind "ipo_by":  openai.ipo_month_index <= month  (null IPO counts as 0)
#   kind "oval_at": openai.valuation_usd_trillions[month] >= thr
MARKETS = [
    {"id": "sp500>6000@2027-12", "kind": "ge_at", "series": "sp500", "month": 18, "thr": 6000, "price": 0.55},
    {"id": "sp500>7500@2030-12", "kind": "ge_at", "series": "sp500", "month": 54, "thr": 7500, "price": 0.42},
    {"id": "btc>150k@2027-12", "kind": "ge_at", "series": "crypto:BTC", "month": 18, "thr": 150_000, "price": 0.50},
    {"id": "btc>300k@2030-12", "kind": "ge_at", "series": "crypto:BTC", "month": 54, "thr": 300_000, "price": 0.32},
    {"id": "cpi>110@2029-12", "kind": "ge_at", "series": "inflation", "month": 42, "thr": 110.0, "price": 0.60},
    {
        "id": "sfhome>115@2030-12",
        "kind": "ge_at",
        "series": f"home_value:{LOC}",
        "month": 54,
        "thr": 115.0,
        "price": 0.45,
    },
    {"id": "sfrent>112@2029-12", "kind": "ge_at", "series": f"rent:{LOC}", "month": 42, "thr": 112.0, "price": 0.55},
    {"id": "openai_ipo<=2029-12", "kind": "ipo_by", "month": 42, "price": 0.55},
    {"id": "openai_val>2T@2030-12", "kind": "oval_at", "month": 54, "thr": 2.0, "price": 0.50},
]

SCENARIOS_PER_CALL = 4
N_CALLS = 4

# Markets only probe out to this month, so a path is evaluable if it covers index 0..MAX_MARKET_MONTH.
# (Dense emission drifts in length — the model rarely lands exactly HORIZON_MONTHS+1 entries; we report
# that as a finding but don't require the full horizon just to score the marginals.)
MAX_MARKET_MONTH = max(m["month"] for m in MARKETS)


def _schema() -> str:
    series_lines = "\n".join(
        f'    "{k}": [{HORIZON_MONTHS + 1} numbers],  // {desc}' for k, desc in LEVEL_SERIES.items()
    )
    return (
        f"Each scenario is one internally-consistent monthly trajectory. EXACTLY these keys:\n"
        f'  "label": short string,\n'
        f'  "paths": {{   // each array has EXACTLY {HORIZON_MONTHS + 1} entries, monthly from 2026-06 (index 0) to 2031-06 (index {HORIZON_MONTHS}); index 0 = the anchor level\n'
        f"{series_lines}\n"
        f"  }},\n"
        f'  "openai": {{\n'
        f'    "ipo_month_index": int or null,  // months from 2026-06 until OpenAI IPOs; null = no IPO within horizon\n'
        f'    "valuation_usd_trillions": [{HORIZON_MONTHS + 1} numbers]  // OpenAI enterprise value in TRILLIONS USD, monthly\n'
        f"  }}\n"
        f"Every array must have exactly {HORIZON_MONTHS + 1} numeric entries. Paths must be smooth month-to-month "
        f"(no teleporting); crashes/booms span several months. Correlated assets co-move (a risk-off month hits "
        f"equities and crypto together; high inflation drags rents and home prices up)."
    )


def _post(endpoint: str, body: dict, tag: str) -> dict:
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
        err = None
    except urllib.error.HTTPError as e:
        resp, err = {"error": e.read().decode()[:1000], "code": e.code}, e.code
    dt = time.time() - t0
    (TRANSCRIPTS / f"{tag}.json").write_text(
        json.dumps({"endpoint": endpoint, "request": body, "response": resp, "latency_s": dt}, indent=2) + "\n"
    )
    if err is not None:
        raise RuntimeError(f"HTTP {err} for {tag}: {resp}")
    return resp


def pick_model() -> tuple[str, str]:
    """Probe CANDIDATES cheapest-first with a 1-token smoke call; return the first (endpoint, model) that answers."""
    for endpoint, model in CANDIDATES:
        body = {"model": model, "messages": [{"role": "user", "content": "reply ok"}], "max_tokens": 4}
        try:
            _post(endpoint, body, f"probe_{model}")
            print(f"picked model={model} endpoint={'general' if endpoint == GENERAL else 'coding'}")
            return endpoint, model
        except RuntimeError as e:
            print(f"  probe {model} unavailable: {e}")
    raise RuntimeError("no candidate model answered")


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


def append_quota_log(
    *, script: str, model: str, endpoint: str, q0: dict, q1: dict, total_tokens: int, prompt_tokens: int | None = None
) -> None:
    """Append one run's token spend + weekly/short quota before-after to results/quota_log.jsonl.

    The z.ai token quota API exposes only an integer percentage (no raw token counts), so burn rate is
    tracked from both sides: the coarse server-side weekly %, and our own precise token totals here.
    """
    rec = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "script": script,
        "model": model,
        "endpoint": "general" if endpoint == GENERAL else "coding",
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "weekly_pct_before": q0.get("weekly_7d_pct"),
        "weekly_pct_after": q1.get("weekly_7d_pct"),
        "short5h_pct_before": q0.get("short_5h_pct"),
        "short5h_pct_after": q1.get("short_5h_pct"),
    }
    with (RESULTS / "quota_log.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def market_prompt(conditioned: bool, nonce: int) -> list[dict]:
    sys = (
        "You generate plausible future macro/market SCENARIOS as dense monthly numeric JSON trajectories. "
        'Output ONLY a JSON object {"scenarios": [ ... ]} and nothing else. '
        f"Produce exactly {SCENARIOS_PER_CALL} DIVERSE scenarios spanning the full plausible range — "
        "optimistic, median, AND pessimistic, explicitly including tail outcomes (multi-month crashes, "
        "AI-driven booms, OpenAI never IPOing, OpenAI IPOing huge). Each scenario must be internally "
        "consistent across series and smooth over time."
    )
    user = f"{ANCHORS}\n\n{_schema()}\n\n(diversity seed {nonce}: make these scenarios different from a typical run.)"
    if conditioned:
        lines = "\n".join(f"  - {m['id']}: probability {m['price']:.2f}" for m in MARKETS)
        user += (
            "\n\nCrowd prediction-market probabilities to honor IN DISTRIBUTION (the fraction of your "
            f"scenarios satisfying each should roughly match):\n{lines}"
        )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def generate(endpoint: str, model: str, conditioned: bool) -> tuple[list[dict], int]:
    variant = "conditioned" if conditioned else "unconditioned"
    scenarios: list[dict] = []
    tokens = 0
    for i in range(N_CALLS):
        body = {
            "model": model,
            "temperature": 1.1,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": market_prompt(conditioned, nonce=i),
        }
        resp = _post(endpoint, body, f"{variant}_call{i:02d}")
        tokens += resp.get("usage", {}).get("total_tokens", 0)
        content = resp["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
            batch = parsed["scenarios"] if isinstance(parsed, dict) else parsed
            scenarios.extend(s for s in batch if isinstance(s, dict))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  [{variant} call{i}] parse failure: {e}")
    return scenarios, tokens


def _path(scn: dict, series: str) -> list[float] | None:
    """Return the dense monthly path for `series` if structurally valid (right length, finite, positive), else None."""
    paths = scn.get("paths")
    if not isinstance(paths, dict):
        return None
    arr = paths.get(series)
    if not isinstance(arr, list) or len(arr) <= MAX_MARKET_MONTH:
        return None
    try:
        vals = [float(x) for x in arr]
    except (TypeError, ValueError):
        return None
    return vals if all(math.isfinite(v) and v > 0 for v in vals) else None


def _oval(scn: dict) -> list[float] | None:
    oa = scn.get("openai")
    if not isinstance(oa, dict):
        return None
    arr = oa.get("valuation_usd_trillions")
    if not isinstance(arr, list) or len(arr) <= MAX_MARKET_MONTH:
        return None
    try:
        vals = [float(x) for x in arr]
    except (TypeError, ValueError):
        return None
    return vals if all(math.isfinite(v) and v > 0 for v in vals) else None


def indicator(scn: dict, m: dict) -> int | None:
    """1/0 if the scenario satisfies the market; None if the scenario lacks a well-formed field."""
    if m["kind"] == "ge_at":
        p = _path(scn, m["series"])
        return None if p is None else int(p[m["month"]] >= m["thr"])
    if m["kind"] == "oval_at":
        p = _oval(scn)
        return None if p is None else int(p[m["month"]] >= m["thr"])
    if m["kind"] == "ipo_by":
        oa = scn.get("openai")
        if not isinstance(oa, dict) or "ipo_month_index" not in oa:
            return None
        ipo = oa["ipo_month_index"]
        if ipo is None:
            return 0
        return int(ipo <= m["month"]) if isinstance(ipo, int) else None
    raise ValueError(m["kind"])


def max_monthly_log_jump(scn: dict) -> float | None:
    """Largest absolute month-over-month log return across the level series — a smoothness/teleport diagnostic."""
    all_jumps: list[float] = []
    for series in LEVEL_SERIES:
        p = _path(scn, series)
        if p is None:
            return None
        all_jumps.extend(abs(math.log(p[t + 1] / p[t])) for t in range(len(p) - 1))
    return max(all_jumps)


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


def _path_lengths(scn: dict) -> list[int]:
    paths = scn.get("paths")
    return [len(v) for v in paths.values() if isinstance(v, list)] if isinstance(paths, dict) else []


def evaluate(scenarios: list[dict], variant: str) -> dict:
    valid = []
    jumps = []
    all_lengths: list[int] = []
    for scn in scenarios:
        all_lengths.extend(_path_lengths(scn))
        inds = [indicator(scn, m) for m in MARKETS]
        if all(v is not None for v in inds):
            valid.append([int(v) for v in inds])
            jump = max_monthly_log_jump(scn)
            if jump is not None:
                jumps.append(jump)
    n = len(valid)
    full = sum(length == HORIZON_MONTHS + 1 for length in all_lengths)
    targets = [m["price"] for m in MARKETS]
    raw = [sum(row[m] for row in valid) / n for m in range(len(MARKETS))] if n else []
    _weights, post, ess = reweight(valid, targets) if n else ([], [], 0.0)
    report = {
        "variant": variant,
        "scenarios_returned": len(scenarios),
        "scenarios_valid": n,
        "ess": ess,
        "ess_frac": ess / n if n else 0.0,
        "horizon_months_expected": HORIZON_MONTHS + 1,
        "paths_total": len(all_lengths),
        "paths_full_length": full,
        "path_len_min": min(all_lengths) if all_lengths else None,
        "path_len_median": sorted(all_lengths)[len(all_lengths) // 2] if all_lengths else None,
        "max_monthly_log_jump_mean": (sum(jumps) / len(jumps)) if jumps else None,
        "max_monthly_log_jump_p95": (sorted(jumps)[int(0.95 * (len(jumps) - 1))]) if jumps else None,
        "markets": [
            {"id": MARKETS[m]["id"], "price": targets[m], "raw": round(raw[m], 3), "reweighted": round(post[m], 3)}
            for m in range(len(MARKETS))
        ]
        if n
        else [],
    }
    print(f"\n=== {variant}: {n}/{len(scenarios)} valid | ESS {ess:.1f} ({report['ess_frac'] * 100:.0f}% of valid) ===")
    print(
        f"  length discipline: {full}/{len(all_lengths)} paths hit full {HORIZON_MONTHS + 1} "
        f"(min {report['path_len_min']}, median {report['path_len_median']})"
    )
    if jumps:
        print(f"  smoothness: mean max monthly |log-return| {report['max_monthly_log_jump_mean']:.2f}")
    print(f"  {'market':22} {'price':>6} {'raw':>6} {'reweighted':>11}")
    for row in report["markets"]:
        print(f"  {row['id']:22} {row['price']:>6.2f} {row['raw']:>6.2f} {row['reweighted']:>11.2f}")
    return report


def main() -> None:
    TRANSCRIPTS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}% short5h={q0.get('short_5h_pct')}%")
    endpoint, model = pick_model()
    total_tokens = 0
    reports = []
    for conditioned in (False, True):
        variant = "conditioned" if conditioned else "unconditioned"
        print(f"\n--- generating {variant} ({N_CALLS} calls x {SCENARIOS_PER_CALL}) ---")
        scenarios, tokens = generate(endpoint, model, conditioned)
        total_tokens += tokens
        (RESULTS / f"{variant}_scenarios.json").write_text(json.dumps(scenarios, indent=2) + "\n")
        reports.append(evaluate(scenarios, variant))
    q1 = quota()
    summary = {
        "model": model,
        "endpoint": "general" if endpoint == GENERAL else "coding",
        "horizon_months": HORIZON_MONTHS,
        "total_tokens": total_tokens,
        "quota_before": q0,
        "quota_after": q1,
        "weekly_pct_delta": (q1.get("weekly_7d_pct") or 0) - (q0.get("weekly_7d_pct") or 0),
        "reports": reports,
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    append_quota_log(script="run_spike", model=model, endpoint=endpoint, q0=q0, q1=q1, total_tokens=total_tokens)
    print(
        f"\n=== model {model} | {total_tokens} tokens | weekly quota {q0.get('weekly_7d_pct')}% -> "
        f"{q1.get('weekly_7d_pct')}% (delta {summary['weekly_pct_delta']}pp) ==="
    )


if __name__ == "__main__":
    main()
