"""Joint kernel: percentiles + samples in one emission (augur/x, throwaway).

Combines the two earlier kernels in a SINGLE call: the model emits (1) per-series predictive quantiles
(p1..p99) — which we've shown it states with honest wide tails — AND (2) N joint samples drawn from
that same distribution. Two motivations, both testable from this one object:

  - The percentile commitment may *scaffold* the samples: having just stated a wide p1/p99, the model
    may draw genuinely wider joint samples instead of hugging the current level (the failure of
    kernel.py / kernel_iid.py).
  - Sklar split, both halves in one call: use the percentiles for the marginals (calibrated) and the
    samples only for the cross-series dependence (their empirical copula) — robust even if the samples
    stay narrow, since we'd take only their co-movement, not their spread.

Single unified emission → one process, scored and sampled identically. Reuses the percentile machinery
(kernel_percentile) for the marginals and kernel.pit for the sample cloud.
"""

from __future__ import annotations

import json
import math

from kernel import SERIES, SERIES_DESC, _call
from kernel_percentile import PERCENTILES, _enforce_monotone


def _sample_instruction(n: int, *, sharp: bool) -> str:
    if not sharp:
        return (
            f'2. "samples": {n} INDEPENDENT joint samples drawn from that SAME distribution. Each sample is one '
            "cross-section over all series; within a sample the series move together coherently, capturing the "
            "realistic relationships between them. Your samples MUST be consistent with your percentiles: across "
            "the samples, each series' spread should match the p1..p99 you stated — typical values common, tail "
            "values rare but present.\n"
        )
    # Sharp: the model defaults to an evenly-spaced quantile GRID; force a density-weighted i.i.d. draw.
    return (
        f'2. "samples": {n} samples that are an HONEST RANDOM (i.i.d.) draw from the distribution you just '
        f"described — as if you actually rolled the dice {n} times. This is NOT a grid and NOT evenly spaced "
        "from low to high: in a real random sample most draws land near the middle and only a few reach the "
        f"tails. Concretely, for EACH series independently, its values across the {n} samples should match the "
        "percentiles you stated for THAT series: about HALF between that series' p25 and p75, about 1 in 10 "
        "beyond its p90 or below its p10, and about 1 in 50 beyond its p99 or below its p1. Near-duplicate "
        "or clustered values are expected and correct; do NOT sort them; do NOT lay one out at each percentile. "
        "Each sample is one joint cross-section — within a sample the series move together coherently, capturing "
        "the realistic relationships between them.\n"
    )


def system_prompt(n: int, *, sharp: bool = False) -> str:
    lines = "\n".join(f"  {s}: {SERIES_DESC[s]}" for s in SERIES)
    grid = ", ".join(str(p) for p in PERCENTILES)
    return (
        "You forecast the NEXT month for several US macro series:\n"
        f"{lines}\n"
        "Output a JSON object with TWO fields:\n"
        f'1. "percentiles": for EACH series, your predictive quantiles at percentiles {grid}. p50 is your '
        "median; p1/p99 are your honest 1-in-100 low/high — make the tails genuinely wide (crashes and "
        "spikes happen), non-decreasing.\n"
        f"{_sample_instruction(n, sharp=sharp)}"
        'Output ONLY JSON: {"percentiles": {series: {"1": num, ..., "99": num}, ...}, "samples": '
        f"[{{series: num, ...}}, ...]}} with exactly {n} samples. Every object must contain all of: "
        f"{', '.join(SERIES)}."
    )


def sample_step(
    endpoint: str,
    model: str,
    history: list[tuple[str, dict[str, float]]],
    next_label: str,
    n_options: int,
    temperature: float,
    tag: str,
    *,
    sharp: bool = False,
) -> tuple[dict[str, dict[float, float]], list[dict], dict]:
    """Returns (percentiles {series:{u:val}}, samples [{"values":{series:float},"weight":1.0}], usage)."""
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
        "messages": [
            {"role": "system", "content": system_prompt(n_options, sharp=sharp)},
            {"role": "user", "content": user},
        ],
    }
    resp = _call(endpoint, body, tag)
    content = resp["choices"][0]["message"]["content"]
    percentiles: dict[str, dict[float, float]] = {}
    samples: list[dict] = []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return percentiles, samples, resp.get("usage", {})
    if not isinstance(parsed, dict):
        return percentiles, samples, resp.get("usage", {})

    raw_pctl = parsed.get("percentiles")
    if isinstance(raw_pctl, dict):
        for s in SERIES:
            raw = raw_pctl.get(s)
            if isinstance(raw, dict):
                try:
                    qmap = {p / 100.0: float(raw[str(p)]) for p in PERCENTILES}
                except (KeyError, TypeError, ValueError):
                    continue
                if all(math.isfinite(v) for v in qmap.values()):
                    percentiles[s] = _enforce_monotone(qmap)

    raw_samples = parsed.get("samples")
    for o in raw_samples if isinstance(raw_samples, list) else []:
        vals_src = o.get("values") if isinstance(o, dict) and isinstance(o.get("values"), dict) else o
        if not isinstance(vals_src, dict):
            continue
        try:
            vals = {s: float(vals_src[s]) for s in SERIES}
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(x) for x in vals.values()):
            samples.append({"values": vals, "weight": 1.0})
    return percentiles, samples, resp.get("usage", {})
