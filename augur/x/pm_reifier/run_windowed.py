"""Windowed (conversational) rollout variant of the PM-reifier spike (augur/x, throwaway).

The one-shot dense run (run_spike.py) showed the model cannot emit fixed-length monthly arrays:
~2/3 of scenarios were dropped because some path didn't reach the furthest market month. This
variant instead drives each world as a CONVERSATION, advancing W months per turn: we own the grid
and concatenate, so length discipline is enforced by us (retry a window that miscounts), not begged
from the model. Each world is its own conversation (independent draw from the base measure Q);
BATCH_SIZE > 1 rolls several deliberately-distinct worlds in one thread as a coverage knob, trading
independence for diversity. Conversations run in parallel.

Reuses the markets + reweight + indicator harness from run_spike. Same augur factor wire-ids
(inflation, sp500, crypto:BTC, home_value:<loc>, rent:<loc>) plus the OpenAI PE issuer.

Run: python3 augur/x/pm_reifier/run_windowed.py
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
    LOC,
    MARKETS,
    RESULTS,
    _post,
    append_quota_log,
    indicator,
    max_monthly_log_jump,
    quota,
    reweight,
)

W = 12  # months advanced per conversation turn (window size)
HORIZON = 60  # months 1..60 produced across ceil(HORIZON/W) windows; index 0 is the anchor
N_CONV = 16  # independent conversations
BATCH_SIZE = 1  # worlds per conversation (1 = pure independent draw; >1 = diverse-batch coverage knob)
CONCURRENCY = 2  # free-tier rate limits are tight; keep low and back off on 429
TEMPERATURE = 1.0  # 1.0 is accepted across GLM models; some free models 400 on temperature > 1.0
OVAL = "openai_valuation_usd_trillions"  # PE issuer path key (in the window payload, not LEVEL_SERIES)

ANCHOR0 = {"inflation": 100.0, "sp500": 5300.0, "crypto:BTC": 95000.0, f"home_value:{LOC}": 100.0, f"rent:{LOC}": 100.0}
ANCHOR0_OVAL = 0.85
WINDOW_SERIES = [*LEVEL_SERIES, OVAL]

# Endpoint+model are chosen once in main() and read by worker threads.
ENDPOINT = ""
MODEL = ""

SYSTEM = (
    "You simulate ONE plausible future world, month by month, as numeric JSON. Series to track each "
    "month: inflation (CPI index, 100.0 at month 0), sp500 (S&P 500 level), crypto:BTC (USD), "
    f"home_value:{LOC} (SF home-price index, 100.0 at m0), rent:{LOC} (SF rent index, 100.0 at m0), "
    f"{OVAL} (OpenAI enterprise value, TRILLIONS USD). I advance the world in windows; each reply is "
    'ONLY a JSON object {"regime": short note on the current regime and what is coming, "months": '
    '{series: [k numbers], ...}, "openai_ipo_month_index": int or null (set ONLY on the window where '
    "OpenAI IPOs)}. The k numbers are the levels at the next k consecutive months. Keep paths smooth "
    "month-to-month, keep series correlated (risk-off hits equities and crypto together; inflation "
    "drags rents and home prices), and maintain the regime arc you establish across windows."
)


def _user_turn(start_month: int, k: int, last_levels: dict[str, float], *, fresh: bool, distinct: bool) -> str:
    levels = ", ".join(f"{s}={last_levels[s]:g}" for s in WINDOW_SERIES)
    if fresh:
        head = (
            "Simulate a DIFFERENT world from month 0, qualitatively distinct from the previous one(s) "
            "in this conversation (different regime, different tails)."
            if distinct
            else "Begin a new world at month 0."
        )
        return f"{head}\nMonth-0 levels: {levels}\nReturn exactly {k} values per series for months 1..{k}."
    return (
        f"Continue the SAME world. Month-{start_month} levels: {levels}\n"
        f"Return exactly {k} values per series for months {start_month + 1}..{start_month + k}."
    )


def _parse_window(content: str, k: int) -> dict | None:
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return None
    months = obj.get("months") if isinstance(obj, dict) else None
    if not isinstance(months, dict):
        return None
    out: dict[str, list[float]] = {}
    for s in WINDOW_SERIES:
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
    ipo = obj.get("openai_ipo_month_index")
    ipo = ipo if isinstance(ipo, int) else None
    return {"months": out, "openai_ipo_month_index": ipo}


def pick_model() -> tuple[str, str]:
    """Probe candidates cheapest-first with the REAL generation params (thinking + json_object), backing
    off on 429, so we skip models that 400 on those params and ride through transient rate limits."""
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


def _call(body: dict, tag: str, max_tries: int = 6) -> dict:
    """_post with exponential backoff on transient failures: the free tier's 429 rate limit and
    socket read timeouts / connection errors (common when the free tier is overloaded)."""
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


def _step(messages: list[dict], user: str, k: int, tag: str, usage: dict) -> dict | None:
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
        parsed = _parse_window(content, k)
        if parsed is not None:
            return parsed
        usage["retries"] += 1
        messages.append(
            {
                "role": "user",
                "content": f"Malformed or wrong count. Return ONLY the JSON with exactly {k} numbers per series.",
            }
        )
    return None


def _assemble(windows: list[dict]) -> dict:
    paths = {s: [ANCHOR0[s]] for s in LEVEL_SERIES}
    oval = [ANCHOR0_OVAL]
    ipo = None
    for win in windows:
        for s in LEVEL_SERIES:
            paths[s].extend(win["months"][s])
        oval.extend(win["months"][OVAL])
        if ipo is None and win["openai_ipo_month_index"] is not None:
            ipo = win["openai_ipo_month_index"]
    return {"paths": paths, "openai": {"ipo_month_index": ipo, "valuation_usd_trillions": oval}}


def rollout_conversation(ci: int) -> dict:
    """Roll out BATCH_SIZE worlds in one thread; return assembled worlds + token/retry accounting."""
    messages = [{"role": "system", "content": f"{SYSTEM}\n(diversity seed {ci}.)"}]
    usage = {"prompt": 0, "completion": 0, "calls": 0, "retries": 0}
    worlds: list[dict | None] = []
    for wi in range(BATCH_SIZE):
        last = {**ANCHOR0, OVAL: ANCHOR0_OVAL}
        windows: list[dict] = []
        ok = True
        try:
            for t in range(math.ceil(HORIZON / W)):
                start = t * W
                k = min(W, HORIZON - start)
                user = _user_turn(start, k, last, fresh=(t == 0), distinct=(wi > 0))
                win = _step(messages, user, k, f"conv{ci:02d}_w{wi}_win{t}", usage)
                if win is None:  # malformed window past retry — drop this world
                    ok = False
                    break
                windows.append(win)
                last = {s: win["months"][s][-1] for s in WINDOW_SERIES}
        except (RuntimeError, OSError) as e:  # API/network error past backoff — isolate, don't sink the run
            print(f"  conv{ci:02d} world{wi} failed: {str(e)[:100]}")
            ok = False
        worlds.append(_assemble(windows) if ok else None)
    return {"worlds": worlds, **usage}


def evaluate(worlds: list[dict], usage: dict) -> dict:
    valid, jumps, lengths = [], [], []
    for w in worlds:
        lengths.extend(len(v) for v in w["paths"].values())
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
        "path_len_min": min(lengths) if lengths else None,
        "max_monthly_log_jump_mean": (sum(jumps) / len(jumps)) if jumps else None,
        "calls": usage["calls"],
        "retries": usage["retries"],
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
        "markets": [
            {"id": MARKETS[m]["id"], "price": targets[m], "raw": round(raw[m], 3), "reweighted": round(post[m], 3)}
            for m in range(len(MARKETS))
        ]
        if n
        else [],
    }
    print(f"\n=== windowed: {n}/{len(worlds)} valid | ESS {ess:.1f} ({report['ess_frac'] * 100:.0f}%) ===")
    print(f"  length discipline: {full}/{len(lengths)} paths at full {HORIZON + 1} (min {report['path_len_min']})")
    print(f"  retries: {usage['retries']} over {usage['calls']} window calls")
    print(f"  tokens: {usage['prompt']} prompt + {usage['completion']} completion")
    if jumps:
        print(f"  smoothness: mean max monthly |log-return| {report['max_monthly_log_jump_mean']:.2f}")
    print(f"  {'market':22} {'price':>6} {'raw':>6} {'reweighted':>11}")
    for row in report["markets"]:
        print(f"  {row['id']:22} {row['price']:>6.2f} {row['raw']:>6.2f} {row['reweighted']:>11.2f}")
    return report


def main() -> None:
    global ENDPOINT, MODEL
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%")
    ENDPOINT, MODEL = pick_model()
    print(f"rolling out {N_CONV} conversations x {BATCH_SIZE} worlds, {W}-month windows, concurrency {CONCURRENCY}")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        convs = list(ex.map(rollout_conversation, range(N_CONV)))
    worlds = [w for c in convs for w in c["worlds"] if w is not None]
    usage = {
        "prompt": sum(c["prompt"] for c in convs),
        "completion": sum(c["completion"] for c in convs),
        "calls": sum(c["calls"] for c in convs),
        "retries": sum(c["retries"] for c in convs),
    }
    report = evaluate(worlds, usage)
    q1 = quota()
    summary = {
        "model": MODEL,
        "window_months": W,
        "horizon_months": HORIZON,
        "batch_size": BATCH_SIZE,
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
        f"\n=== model {MODEL} | {usage['prompt'] + usage['completion']} tokens "
        f"({usage['prompt']} prompt) | weekly {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}% "
        f"(delta {summary['weekly_pct_delta']}pp) ==="
    )


if __name__ == "__main__":
    main()
