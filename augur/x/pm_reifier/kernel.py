"""Shared per-step kernel for the PM-reifier (augur/x, throwaway).

The LLM as a stochastic transition kernel: given recent monthly history, return N weighted JOINT
options for the next month — each option is one cross-section over all macro series. Used by both
forward sampling and the calibration backtest so the proposal lives in exactly one place. Macro-only
(the series we have ground truth for); discrete OpenAI events belong to the forward sampler, not the
backtest. Low-level API plumbing (`_post`, endpoints) is reused from run_spike.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error

from run_spike import _post

SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
SERIES_DESC = {
    "inflation": "CPI index (a cumulative price level that climbs over time)",
    "sp500": "S&P 500 index level",
    "crypto:BTC": "Bitcoin price in USD",
    "home_value:sf_ca": "San Francisco home-price index",
    "rent:sf_ca": "San Francisco rent index",
}


def system_prompt(n: int) -> str:
    lines = "\n".join(f"  {s}: {SERIES_DESC[s]}" for s in SERIES)
    return (
        "You forecast the NEXT month for several US macro series, expressed as a SAMPLE of possible "
        f"outcomes. Series:\n{lines}\n"
        f'Given the recent monthly history, output ONLY JSON: {{"options": [{{"values": {{series: '
        f'number, ...}}, "weight": number}}, ...]}} with exactly {n} options. The options are diverse '
        "draws spanning your FULL belief about next month — include calm, up, down, AND tail moves "
        "(crashes, spikes), in proportion to how likely each is. weight is each option's rough "
        "probability (weights ~sum to 1). Each option is ONE joint cross-section: all series move "
        "together coherently (a risk-off month hits equities and crypto together; inflation drags "
        f"rents and home prices). Every option must contain all of: {', '.join(SERIES)}."
    )


def _call(endpoint: str, body: dict, tag: str, max_tries: int = 8) -> dict:
    """_post with exponential backoff on 429 + any 5xx + socket timeouts/connection errors."""
    delay = 2.0
    last: Exception = RuntimeError("unreachable")
    for _ in range(max_tries):
        try:
            return _post(endpoint, body, tag)
        except RuntimeError as e:
            s = str(e)
            if "HTTP 429" not in s and "HTTP 5" not in s:
                raise
            last = e
        except (TimeoutError, urllib.error.URLError) as e:
            last = e
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise last


def sample_step(
    endpoint: str,
    model: str,
    history: list[tuple[str, dict[str, float]]],
    next_label: str,
    n_options: int,
    temperature: float,
    tag: str,
) -> tuple[list[dict], dict]:
    """Ask the model for N weighted joint next-month options given the history (oldest first, last = now).

    Returns (options, usage); each option is {"values": {series: float}, "weight": float}.
    """
    cur_label = history[-1][0]
    block = "\n".join("  " + s + ": " + ", ".join(f"{vals[s]:g}" for _, vals in history) for s in SERIES)
    user = (
        f"Recent monthly history (oldest first; the last value of each row = current month {cur_label}):\n"
        f"{block}\nForecast the macro cross-section for the NEXT month ({next_label})."
    )
    body = {
        "model": model,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system_prompt(n_options)}, {"role": "user", "content": user}],
    }
    resp = _call(endpoint, body, tag)
    content = resp["choices"][0]["message"]["content"]
    options: list[dict] = []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return options, resp.get("usage", {})
    for o in parsed.get("options", []) if isinstance(parsed, dict) else []:
        if not isinstance(o, dict) or not isinstance(o.get("values"), dict):
            continue
        try:
            vals = {s: float(o["values"][s]) for s in SERIES}
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(x) for x in vals.values()):
            w = o.get("weight", 1.0)
            options.append({"values": vals, "weight": float(w) if isinstance(w, int | float) else 1.0})
    return options, resp.get("usage", {})


def pit(options: list[dict], realized: float, series: str) -> float | None:
    """Weighted model-CDF at the realized value: the probability mass the kernel placed at or below it.

    Uniform over [0,1] across many steps iff the kernel is calibrated for that series. Mass at 0 / 1
    (realized below / above every option) accumulating = thin tails (overconfident).
    """
    tw = sum(o["weight"] for o in options)
    if tw <= 0:
        return None
    below = sum(o["weight"] for o in options if o["values"][series] <= realized)
    return below / tw


def draw(options: list[dict], u: float) -> dict:
    """Inverse-CDF draw of one option by weight (for the forward sampler); u in [0,1)."""
    tw = sum(o["weight"] for o in options)
    c = 0.0
    for o in options:
        c += o["weight"] / tw
        if u < c:
            return o
    return options[-1]
