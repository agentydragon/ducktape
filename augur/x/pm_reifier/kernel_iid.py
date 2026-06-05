"""IID-reframed enumeration kernel for the PM-reifier (augur/x, throwaway).

Hypothesis (why this exists): the enumeration kernel (`kernel.py`) was measured miscalibrated — biased
high + thin-tailed — and the percentile kernel (marginal-only, not deployable) was not. One explanation
is that `kernel.py`'s prompt doesn't actually ask for an i.i.d. sample of the model's belief: it says
"diverse draws ... include calm, up, down, AND tail moves," which the model may read as "hand-pick a
spread of notable scenarios" (salience-weighted, mode-collapsed) rather than "sample from your
predictive." This kernel reframes the ask as literal i.i.d. sampling from the model's own predictive
distribution, and optionally enables thinking so the model reasons about that distribution first.

Same emission shape as `kernel.py` (N equally-weighted joint cross-sections), so it stays a JOINT and a
single unified process — scored and sampled identically — and reuses `kernel.pit` / `kernel.draw`.
"""

from __future__ import annotations

import json
import math

from kernel import SERIES, SERIES_DESC, _call


def system_prompt(n: int) -> str:
    lines = "\n".join(f"  {s}: {SERIES_DESC[s]}" for s in SERIES)
    return (
        "You forecast the NEXT month for several US macro series:\n"
        f"{lines}\n"
        "First, form your full predictive probability distribution over next month's joint outcome — your "
        "honest belief about what could happen, with all its uncertainty. Then draw "
        f"{n} INDEPENDENT samples from that distribution: an i.i.d. sample, exactly as if you sampled "
        f"{n} times from the true distribution. This is NOT a hand-picked spread of distinct scenarios — "
        "typical outcomes should appear often and rare outcomes (crashes, spikes) should appear rarely, in "
        "proportion to their actual probability, so the empirical frequencies of your samples match your "
        "beliefs. Each sample is ONE joint cross-section: all series move together coherently within a "
        "sample (a risk-off month hits equities and crypto together; inflation drags rents and home "
        f'prices). Output ONLY JSON: {{"samples": [{{series: number, ...}}, ...]}} with exactly {n} '
        f"samples, each containing all of: {', '.join(SERIES)}."
    )


def _extract_json(content: str) -> dict | None:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Thinking mode can wrap the JSON in prose/fences; grab the outermost object.
    start, end = content.find("{"), content.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def sample_step(
    endpoint: str,
    model: str,
    history: list[tuple[str, dict[str, float]]],
    next_label: str,
    n_options: int,
    temperature: float,
    tag: str,
    *,
    thinking: bool = False,
) -> tuple[list[dict], dict]:
    """Ask for N i.i.d. joint samples of the next month. Returns (options, usage); options are equally
    weighted {"values": {series: float}, "weight": 1.0} — same shape as kernel.sample_step."""
    cur_label = history[-1][0]
    block = "\n".join("  " + s + ": " + ", ".join(f"{vals[s]:g}" for _, vals in history) for s in SERIES)
    user = (
        f"Recent monthly history (oldest first; the last value of each row = current month {cur_label}):\n"
        f"{block}\nForecast the macro cross-section for the NEXT month ({next_label})."
    )
    body = {
        "model": model,
        "temperature": temperature,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "messages": [{"role": "system", "content": system_prompt(n_options)}, {"role": "user", "content": user}],
    }
    if not thinking:
        # Thinking + forced json_object conflict on z.ai; in thinking mode we extract the JSON ourselves.
        body["response_format"] = {"type": "json_object"}
    resp = _call(endpoint, body, tag)
    content = resp["choices"][0]["message"]["content"]
    options: list[dict] = []
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return options, resp.get("usage", {})
    raw = parsed.get("samples") or parsed.get("options") or []
    for o in raw if isinstance(raw, list) else []:
        # Accept both {series: number} and {"values": {series: number}} shapes.
        vals_src = o.get("values") if isinstance(o, dict) and isinstance(o.get("values"), dict) else o
        if not isinstance(vals_src, dict):
            continue
        try:
            vals = {s: float(vals_src[s]) for s in SERIES}
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(x) for x in vals.values()):
            options.append({"values": vals, "weight": 1.0})
    return options, resp.get("usage", {})
