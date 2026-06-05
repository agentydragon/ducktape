"""Percentile (quantile-function) per-step kernel for the PM-reifier (augur/x, throwaway).

An alternative to the N-weighted-options kernel: instead of enumerating diverse joint draws, ask the
LLM for its predictive **quantiles** of each series' next-month value on a fixed percentile grid
(p1..p99). This is inverse-transform sampling via the LLM, and for the calibration backtest it is
strictly cleaner than enumeration:

  - PIT comes for free: PIT(realized) is the u at which the realized value sits in the quantile
    function (piecewise-linear inverse), with no self-reported weights and no N-resolution noise.
  - Tail coverage is named explicitly: the model has to commit to a p1/p99, so "is its stated 98%
    interval wide enough" becomes directly measurable (realized below p1 / above p99 = definite escape).

The joint cross-series coupling a quantile can't express is irrelevant here — the backtest scores
per-series *marginal* PITs anyway. Reuses SERIES/SERIES_DESC and the backoff caller from `kernel`.
"""

from __future__ import annotations

import itertools
import json
import math

from kernel import SERIES, SERIES_DESC, _call

# Fixed percentile grid (the quantile function we elicit). p10/p90 bound the tail-escape deciles; p1/p99
# are the explicit tail commitments. Stored as fractions u in (0,1).
PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)
US = tuple(p / 100.0 for p in PERCENTILES)


def system_prompt() -> str:
    lines = "\n".join(f"  {s}: {SERIES_DESC[s]}" for s in SERIES)
    grid = ", ".join(str(p) for p in PERCENTILES)
    return (
        "You forecast the NEXT month for several US macro series, expressed as PREDICTIVE QUANTILES of "
        f"each series' next-month value. Series:\n{lines}\n"
        f"For EACH series give the quantiles at these percentiles: {grid}. p50 is your median; p1 is the "
        "value you would undershoot only about 1 month in 100, p99 the value you would exceed only about "
        "1 month in 100. Be HONEST about uncertainty: most months are calm, but crashes and spikes happen, "
        "so p1 and p99 must be genuinely far from the median — a too-narrow p1..p99 is overconfident. "
        "Quantiles must be non-decreasing (p1 <= p5 <= ... <= p99). "
        'Output ONLY JSON: {"quantiles": {series: {"1": number, "5": number, ..., "99": number}, ...}}. '
        f"Include all of: {', '.join(SERIES)}."
    )


def sample_step(
    endpoint: str,
    model: str,
    history: list[tuple[str, dict[str, float]]],
    next_label: str,
    temperature: float,
    tag: str,
) -> tuple[dict[str, dict[float, float]], dict]:
    """Ask for each series' predictive quantile function for the next month.

    Returns ({series: {u: value} monotone-enforced}, usage). A series is omitted if it failed to parse.
    """
    cur_label = history[-1][0]
    block = "\n".join("  " + s + ": " + ", ".join(f"{vals[s]:g}" for _, vals in history) for s in SERIES)
    user = (
        f"Recent monthly history (oldest first; the last value of each row = current month {cur_label}):\n"
        f"{block}\nForecast the predictive quantiles for the NEXT month ({next_label})."
    )
    body = {
        "model": model,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}],
    }
    resp = _call(endpoint, body, tag)
    content = resp["choices"][0]["message"]["content"]
    out: dict[str, dict[float, float]] = {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return out, resp.get("usage", {})
    quantiles = parsed.get("quantiles") if isinstance(parsed, dict) else None
    if not isinstance(quantiles, dict):
        return out, resp.get("usage", {})
    for s in SERIES:
        raw = quantiles.get(s)
        if not isinstance(raw, dict):
            continue
        try:
            qmap = {p / 100.0: float(raw[str(p)]) for p in PERCENTILES}
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in qmap.values()):
            out[s] = _enforce_monotone(qmap)
    return out, resp.get("usage", {})


def _enforce_monotone(qmap: dict[float, float]) -> dict[float, float]:
    """Clamp the elicited quantiles to be non-decreasing in u (LLMs occasionally invert)."""
    running = -math.inf
    fixed: dict[float, float] = {}
    for u in sorted(qmap):
        running = max(running, qmap[u])
        fixed[u] = running
    return fixed


def monotone_violations(raw: dict[float, float]) -> int:
    """Count adjacent inversions in the elicited quantiles (diagnostic; call on the pre-clamp map)."""
    return sum(1 for a, b in itertools.pairwise(sorted(raw)) if raw[b] < raw[a])


def pit(qmap: dict[float, float], realized: float) -> float:
    """PIT via the inverse quantile function: the u where the realized value sits in the elicited CDF.

    Piecewise-linear between grid points. Realized below the stated p1 -> 0.0, above p99 -> 1.0
    (a definite tail escape: the model's most extreme committed quantile still wasn't extreme enough).
    """
    us = sorted(qmap)
    vals = [qmap[u] for u in us]
    if realized <= vals[0]:
        return 0.0
    if realized >= vals[-1]:
        return 1.0
    for i in range(len(us) - 1):
        if vals[i] <= realized <= vals[i + 1]:
            span = vals[i + 1] - vals[i]
            frac = 0.0 if span == 0 else (realized - vals[i]) / span
            return us[i] + frac * (us[i + 1] - us[i])
    return 1.0


def draw(qmap: dict[float, float], u: float) -> float:
    """Inverse-transform draw: the elicited value at percentile u (piecewise-linear). For the forward sampler."""
    us = sorted(qmap)
    vals = [qmap[v] for v in us]
    if u <= us[0]:
        return vals[0]
    if u >= us[-1]:
        return vals[-1]
    for i in range(len(us) - 1):
        if us[i] <= u <= us[i + 1]:
            frac = (u - us[i]) / (us[i + 1] - us[i])
            return vals[i] + frac * (vals[i + 1] - vals[i])
    return vals[-1]
