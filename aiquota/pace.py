from aiquota.models import PaceResult, QuotaWindow

STABLE_FRACTION = 0.05
EXHAUSTED_PERCENT = 100.0


def is_exhausted(w: QuotaWindow) -> bool:
    return w.used_percent >= EXHAUSTED_PERCENT


def compute_pace(w: QuotaWindow) -> PaceResult | None:
    elapsed_secs = w.window_seconds - w.reset_seconds
    elapsed_frac = elapsed_secs / w.window_seconds
    expected_percent = elapsed_frac * 100
    deviation = w.used_percent - expected_percent
    projected_at_reset = None
    seconds_to_exhaust = None
    if elapsed_secs > 0 and w.used_percent > 0:
        rate_per_sec = w.used_percent / elapsed_secs
        seconds_to_exhaust = (100 - w.used_percent) / rate_per_sec
        projected_at_reset = w.used_percent + rate_per_sec * w.reset_seconds
    stable = STABLE_FRACTION < elapsed_frac < 1 - STABLE_FRACTION
    return PaceResult(
        deviation=deviation, projected_at_reset=projected_at_reset, seconds_to_exhaust=seconds_to_exhaust, stable=stable
    )


# Pace deviation thresholds (percentage points: used% - expected%).
PACE_COOL_BELOW = -10
PACE_WARN_ABOVE = 5
PACE_HOT_ABOVE = 15
SHORT_WIN_HOT_PERCENT = 85

TINT_RANK = {"unknown": 0, "stale": 0, "ok": 1, "cool": 1, "warn": 2, "hot": 3}


def tint_for(pace: PaceResult | None, used_percent: float | None, *, is_short: bool) -> str:
    if used_percent is None:
        return "unknown"
    if is_short and used_percent >= SHORT_WIN_HOT_PERCENT:
        return "hot"
    if not pace or not pace.stable:
        if used_percent >= 95:
            return "hot"
        if used_percent >= 80:
            return "warn"
        return "ok"
    if pace.deviation >= PACE_HOT_ABOVE:
        return "hot"
    if pace.deviation >= PACE_WARN_ABOVE:
        return "warn"
    if pace.deviation <= PACE_COOL_BELOW:
        return "cool"
    return "ok"


def binding_tint(short_tint: str, long_tint: str) -> str:
    if short_tint == "hot":
        return "hot"
    return short_tint if TINT_RANK.get(short_tint, 0) > TINT_RANK.get(long_tint, 0) else long_tint
