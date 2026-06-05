"""Windowed (conversational) rollout variant of the PM-reifier spike (augur/x, throwaway).

Each world is a CONVERSATION advancing W months per turn; we concatenate fixed-size windows so the
horizon length is enforced by us (retry a window that miscounts), not begged from the model. Each
world is its own conversation (independent draw from Q); BATCH_SIZE>1 rolls distinct worlds per thread
as a coverage knob. Conversations run in parallel with 429/timeout backoff.

Two grounding changes over the dense one-shot run:
  - Macro series are seeded with a REAL recent-history tail (run fetch_real_history.py -> real_history.json):
    sp500, crypto:BTC (Yahoo), inflation/home_value/rent (FRED), so worlds start from real levels and
    carry real momentum forward.
  - OpenAI is modelled as augur's PE issuer is: discrete EVENTS (primary_round / secondary_tender / ipo
    / collapse with a post-money valuation), not a smooth monthly mark. The model is fed OpenAI's PUBLIC
    funding history (openai_history.json) and emits future events per window. Markets evaluate on the
    event list (IPO by month, valuation at month, a tender by month).

Reuses the reweight/_path harness from run_spike. Run: python3 augur/x/pm_reifier/run_windowed.py
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from run_spike import (
    CANDIDATES,
    GENERAL,
    LEVEL_SERIES,
    RESULTS,
    _path,
    _post,
    append_quota_log,
    max_monthly_log_jump,
    quota,
    reweight,
)

HERE = RESULTS.parent
REAL = json.loads((HERE / "real_history.json").read_text())
OPENAI = json.loads((HERE / "openai_history.json").read_text())

W = 12  # months advanced per conversation turn
HORIZON = 60  # months 1..60 produced across ceil(HORIZON/W) windows; index 0 is the anchor
N_CONV = 16
BATCH_SIZE = 1  # worlds per conversation (1 = independent draw; >1 = diverse-batch coverage knob)
CONCURRENCY = 2  # free-tier rate limits are tight; keep low and back off on 429
TEMPERATURE = 1.0  # 1.0 is accepted across GLM models; some free models 400 on temperature > 1.0

HISTORY: dict[str, list[float]] = REAL["series"]  # real recent tail per macro series (oldest first, last = now)
HISTORY_MONTHS = len(next(iter(HISTORY.values())))
ANCHOR0 = {s: HISTORY[s][-1] for s in LEVEL_SERIES}  # month-0 levels = last real point
ANCHOR_VAL_B = OPENAI["anchor_valuation_usd_b"]  # OpenAI post-money valuation at month 0 ($B)
OAI_KINDS = {"primary_round", "secondary_tender", "ipo", "collapse"}

# Markets recalibrated to the REAL anchors (sp500 ~7.6k, BTC ~63k, indices=100). Macro: ge_at on the
# dense path. OpenAI: ipo_by / oval_at (valuation from the event marks) / tender_by (any secondary
# tender). Month indices: 2027-12=m18, 2029-12=m42, 2030-12=m54. Prices are illustrative.
MARKETS = [
    {"id": "sp500>9000@2027-12", "kind": "ge_at", "series": "sp500", "month": 18, "thr": 9000, "price": 0.50},
    {"id": "sp500>12000@2030-12", "kind": "ge_at", "series": "sp500", "month": 54, "thr": 12000, "price": 0.40},
    {"id": "btc>120k@2027-12", "kind": "ge_at", "series": "crypto:BTC", "month": 18, "thr": 120_000, "price": 0.45},
    {"id": "btc>250k@2030-12", "kind": "ge_at", "series": "crypto:BTC", "month": 54, "thr": 250_000, "price": 0.30},
    {"id": "cpi>110@2029-12", "kind": "ge_at", "series": "inflation", "month": 42, "thr": 110.0, "price": 0.60},
    {
        "id": "sfhome>115@2030-12",
        "kind": "ge_at",
        "series": "home_value:sf_ca",
        "month": 54,
        "thr": 115.0,
        "price": 0.45,
    },
    {"id": "sfrent>112@2029-12", "kind": "ge_at", "series": "rent:sf_ca", "month": 42, "thr": 112.0, "price": 0.55},
    {"id": "openai_tender_by_2028-06", "kind": "oai_tender_by", "month": 24, "price": 0.80},
    {"id": "openai_ipo_by_2029-12", "kind": "oai_ipo_by", "month": 42, "price": 0.45},
    {"id": "openai_val>2T@2030-12", "kind": "oai_val_at", "month": 54, "thr": 2000.0, "price": 0.50},
]
MAX_MARKET_MONTH = max(m["month"] for m in MARKETS)

# Endpoint+model chosen once in main() and read by worker threads.
ENDPOINT = ""
MODEL = ""

SYSTEM = (
    "You simulate ONE plausible future world, month by month, as numeric JSON. Macro series tracked each "
    "month: inflation (CPI index, 100.0 at month 0), sp500 (S&P 500 level), crypto:BTC (USD), "
    "home_value:sf_ca (SF home-price index, 100.0 at m0), rent:sf_ca (SF rent index, 100.0 at m0). "
    "Separately, OpenAI is a private company whose value moves in discrete EVENTS, not a smooth path: "
    "primary_round (new funding), secondary_tender (employees/early holders sell at a set valuation), "
    "ipo (goes public), or collapse. You are given a real recent macro history and OpenAI's public "
    "funding history; continue the world FORWARD from month 0 (= now), carrying recent momentum and "
    "volatility. Each reply is ONLY a JSON object: "
    '{"regime": short note on the regime and what is coming, '
    '"months": {macro_series: [k numbers], ...}, '
    '"openai_events": [{"month": int, "kind": "primary_round"|"secondary_tender"|"ipo"|"collapse", '
    '"valuation_usd_b": number, "price_per_share": number (optional)}, ...]}. '
    "months holds the next k consecutive macro levels; openai_events lists any OpenAI events in those k "
    "months (often empty — tenders historically recur roughly every 6-12 months). Keep macro paths smooth "
    "and correlated (risk-off hits equities and crypto together; inflation drags rents and home prices), "
    "make OpenAI events cohere with the macro regime (a risk-off window slows or cancels rounds), and "
    "maintain the regime arc across windows."
)


def _macro_history_block() -> str:
    return "\n".join(f"  {s}: {', '.join(format(v, 'g') for v in HISTORY[s])}" for s in LEVEL_SERIES)


def _openai_history_block() -> str:
    rows = ", ".join(f"{e['date']} ${e['valuation_usd_b']}B ({e['kind']})" for e in OPENAI["events"])
    return (
        f"OpenAI public funding history (post-money, USD billions): {rows}. Now (month 0): ~${ANCHOR_VAL_B}B, private."
    )


def _user_turn(
    start_month: int, k: int, last_levels: dict[str, float], last_val_b: float, *, fresh: bool, distinct: bool
) -> str:
    if fresh:
        head = (
            "Simulate a DIFFERENT world from month 0, qualitatively distinct from the previous one(s) in "
            "this conversation (different regime, different tails)."
            if distinct
            else "Begin a new world at month 0."
        )
        return (
            f"{head}\nReal recent {HISTORY_MONTHS}-month macro history per series (oldest first; last = month 0 = now):\n"
            f"{_macro_history_block()}\n{_openai_history_block()}\n"
            f"Continue this world forward. Return exactly {k} macro values per series for months 1..{k}, "
            f"and openai_events for any OpenAI events in months 1..{k}."
        )
    levels = ", ".join(f"{s}={last_levels[s]:g}" for s in LEVEL_SERIES)
    return (
        f"Continue the SAME world. Month-{start_month} macro levels: {levels}. Last OpenAI valuation: ~${last_val_b:g}B.\n"
        f"Return exactly {k} macro values per series for months {start_month + 1}..{start_month + k}, "
        f"and openai_events for months {start_month + 1}..{start_month + k}."
    )


def _parse_window(content: str, start: int, k: int) -> dict | None:
    """Validate one window: k numeric levels per macro series + a well-formed (possibly empty) event list."""
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return None
    months = obj.get("months") if isinstance(obj, dict) else None
    if not isinstance(months, dict):
        return None
    out: dict[str, list[float]] = {}
    for s in LEVEL_SERIES:
        arr = months.get(s)
        if not isinstance(arr, list) or len(arr) != k:
            return None
        try:
            vals = [float(x) for x in arr]
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) and v > 0 for v in vals):
            return None
        out[s] = vals
    events: list[dict] = []
    raw_events = obj.get("openai_events", [])
    if not isinstance(raw_events, list):
        return None
    for e in raw_events:
        if not isinstance(e, dict) or e.get("kind") not in OAI_KINDS:
            return None
        month, val = e.get("month"), e.get("valuation_usd_b")
        if not isinstance(month, int) or not (start < month <= start + k):
            return None
        if not isinstance(val, int | float) or not (math.isfinite(val) and val > 0):
            return None
        events.append({"month": month, "kind": e["kind"], "valuation_usd_b": float(val)})
    return {"months": out, "events": events}


def _call(body: dict, tag: str, max_tries: int = 6) -> dict:
    """_post with exponential backoff on transient failures: 429 rate limits and socket/connection errors."""
    delay = 2.0
    last: Exception = RuntimeError("unreachable")
    for _ in range(max_tries):
        try:
            return _post(ENDPOINT, body, tag)
        except RuntimeError as e:
            if "HTTP 429" not in str(e):
                raise
            last = e
        except (TimeoutError, urllib.error.URLError) as e:
            last = e
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise last


def pick_model() -> tuple[str, str]:
    """Probe candidates in preference order with the REAL generation params, backing off on 429."""
    for endpoint, model in CANDIDATES:
        body = {
            "model": model,
            "temperature": TEMPERATURE,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": 'reply {"ok": true}'}],
            "max_tokens": 20,
        }
        delay = 2.0
        for i in range(5):
            try:
                _post(endpoint, body, f"probe_{model}")
                print(f"picked model={model} endpoint={'general' if endpoint == GENERAL else 'coding'}")
                return endpoint, model
            except RuntimeError as e:
                if "HTTP 429" in str(e) and i < 4:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                print(f"  probe {model} unavailable: {str(e)[:120]}")
                break
    raise RuntimeError("no candidate model answered")


def _step(messages: list[dict], user: str, start: int, k: int, tag: str, usage: dict) -> dict | None:
    """One window turn with one retry on a malformed/miscounted reply; appends to the live thread."""
    messages.append({"role": "user", "content": user})
    for attempt in range(2):
        body = {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        resp = _call(body, f"{tag}_a{attempt}")
        u = resp.get("usage", {})
        usage["prompt"] += u.get("prompt_tokens", 0)
        usage["completion"] += u.get("completion_tokens", 0)
        usage["calls"] += 1
        content = resp["choices"][0]["message"]["content"]
        messages.append({"role": "assistant", "content": content})
        parsed = _parse_window(content, start, k)
        if parsed is not None:
            return parsed
        usage["retries"] += 1
        messages.append(
            {
                "role": "user",
                "content": f"Malformed or wrong count. Return ONLY the JSON with exactly {k} numbers per macro series.",
            }
        )
    return None


def _assemble(windows: list[dict]) -> dict:
    paths = {s: [ANCHOR0[s]] for s in LEVEL_SERIES}
    events: list[dict] = []
    for win in windows:
        for s in LEVEL_SERIES:
            paths[s].extend(win["months"][s])
        events.extend(win["events"])
    return {"paths": paths, "openai_events": sorted(events, key=lambda e: e["month"])}


def _oai_val_at(world: dict, month: int) -> float:
    prior = [e for e in world["openai_events"] if e["month"] <= month]
    return prior[-1]["valuation_usd_b"] if prior else ANCHOR_VAL_B


def indicator(world: dict, m: dict) -> int | None:
    if m["kind"] == "ge_at":
        p = _path(world, m["series"])
        return None if p is None else int(p[m["month"]] >= m["thr"])
    ev = world["openai_events"]
    if m["kind"] == "oai_ipo_by":
        return int(any(e["kind"] == "ipo" and e["month"] <= m["month"] for e in ev))
    if m["kind"] == "oai_tender_by":
        return int(any(e["kind"] == "secondary_tender" and e["month"] <= m["month"] for e in ev))
    if m["kind"] == "oai_val_at":
        return int(_oai_val_at(world, m["month"]) >= m["thr"])
    raise ValueError(m["kind"])


def rollout_conversation(ci: int) -> dict:
    messages = [{"role": "system", "content": f"{SYSTEM}\n(diversity seed {ci}.)"}]
    usage = {"prompt": 0, "completion": 0, "calls": 0, "retries": 0}
    worlds: list[dict | None] = []
    for wi in range(BATCH_SIZE):
        last = dict(ANCHOR0)
        last_val_b = float(ANCHOR_VAL_B)
        windows: list[dict] = []
        ok = True
        try:
            for t in range(math.ceil(HORIZON / W)):
                start = t * W
                k = min(W, HORIZON - start)
                user = _user_turn(start, k, last, last_val_b, fresh=(t == 0), distinct=(wi > 0))
                win = _step(messages, user, start, k, f"conv{ci:02d}_w{wi}_win{t}", usage)
                if win is None:
                    ok = False
                    break
                windows.append(win)
                last = {s: win["months"][s][-1] for s in LEVEL_SERIES}
                if win["events"]:
                    last_val_b = win["events"][-1]["valuation_usd_b"]
        except (RuntimeError, OSError) as e:  # API/network error past backoff — isolate, don't sink the run
            print(f"  conv{ci:02d} world{wi} failed: {str(e)[:100]}")
            ok = False
        worlds.append(_assemble(windows) if ok else None)
    return {"worlds": worlds, **usage}


def evaluate(worlds: list[dict], usage: dict) -> dict:
    valid, jumps, lengths, n_events = [], [], [], 0
    for w in worlds:
        lengths.extend(len(v) for v in w["paths"].values())
        n_events += len(w["openai_events"])
        inds = [indicator(w, m) for m in MARKETS]
        if all(v is not None for v in inds):
            valid.append([int(v) for v in inds])
            jump = max_monthly_log_jump(w)
            if jump is not None:
                jumps.append(jump)
    n = len(valid)
    targets = [m["price"] for m in MARKETS]
    raw = [sum(row[m] for row in valid) / n for m in range(len(MARKETS))] if n else []
    _weights, post, ess = reweight(valid, targets) if n else ([], [], 0.0)
    full = sum(length == HORIZON + 1 for length in lengths)
    report = {
        "worlds": len(worlds),
        "valid": n,
        "ess": ess,
        "ess_frac": ess / n if n else 0.0,
        "paths_full_length": full,
        "paths_total": len(lengths),
        "openai_events_total": n_events,
        "calls": usage["calls"],
        "retries": usage["retries"],
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
        "max_monthly_log_jump_mean": (sum(jumps) / len(jumps)) if jumps else None,
        "markets": [
            {"id": MARKETS[m]["id"], "price": targets[m], "raw": round(raw[m], 3), "reweighted": round(post[m], 3)}
            for m in range(len(MARKETS))
        ]
        if n
        else [],
    }
    print(f"\n=== windowed: {n}/{len(worlds)} valid | ESS {ess:.1f} ({report['ess_frac'] * 100:.0f}%) ===")
    print(
        f"  length: {full}/{len(lengths)} paths full {HORIZON + 1} | {n_events} OpenAI events over {len(worlds)} worlds"
    )
    print(
        f"  retries: {usage['retries']} / {usage['calls']} calls | tokens: {usage['prompt']} prompt + {usage['completion']} completion"
    )
    print(f"  {'market':26} {'price':>6} {'raw':>6} {'reweighted':>11}")
    for row in report["markets"]:
        print(f"  {row['id']:26} {row['price']:>6.2f} {row['raw']:>6.2f} {row['reweighted']:>11.2f}")
    return report


def main() -> None:
    global ENDPOINT, MODEL
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(
        f"quota before: weekly={q0.get('weekly_7d_pct')}%  | anchors: "
        + ", ".join(f"{s}={ANCHOR0[s]:g}" for s in LEVEL_SERIES)
    )
    ENDPOINT, MODEL = pick_model()
    print(f"rolling out {N_CONV} conversations x {BATCH_SIZE} worlds, {W}-month windows, concurrency {CONCURRENCY}")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        convs = list(ex.map(rollout_conversation, range(N_CONV)))
    worlds = [w for c in convs for w in c["worlds"] if w is not None]
    usage = {k: sum(c[k] for c in convs) for k in ("prompt", "completion", "calls", "retries")}
    report = evaluate(worlds, usage)
    q1 = quota()
    summary = {
        "model": MODEL,
        "endpoint": "general" if ENDPOINT == GENERAL else "coding",
        "window_months": W,
        "horizon_months": HORIZON,
        "batch_size": BATCH_SIZE,
        "as_of": REAL["as_of"],
        "anchors": ANCHOR0,
        "worlds_failed": N_CONV * BATCH_SIZE - len(worlds),
        "quota_before": q0,
        "quota_after": q1,
        "weekly_pct_delta": (q1.get("weekly_7d_pct") or 0) - (q0.get("weekly_7d_pct") or 0),
        "report": report,
    }
    (RESULTS / "windowed_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    append_quota_log(
        script="run_windowed",
        model=MODEL,
        endpoint=ENDPOINT,
        q0=q0,
        q1=q1,
        total_tokens=usage["prompt"] + usage["completion"],
        prompt_tokens=usage["prompt"],
    )
    print(
        f"\n=== model {MODEL} | {usage['prompt'] + usage['completion']} tokens ({usage['prompt']} prompt) | "
        f"weekly {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}% (delta {summary['weekly_pct_delta']}pp) ==="
    )


if __name__ == "__main__":
    main()
