"""Leakage-free percentile-kernel calibration on a local llama3.1:8b via ollama (augur/x, throwaway).

glm-4.5's June-2024 cutoff is fuzzy/leaky. llama3.1:8b has a documented training cutoff of December
2023 and open weights, so 2024-01 onward is a HARD leakage-free out-of-sample window. We probe the
cleanest signal — the model's stated quantiles (kernel_percentile) — since the heavy sharp-joint schema
is impractical at CPU speed (~4.7 tok/s). Same teacher-forced one-step PIT scoring as backtest_percentile,
but anchored at 2023-12 and routed to ollama's native /api/chat with format=json (the OpenAI-compat
json_object path 500s on this model).

Run (ollama serving llama3.1:8b on :11434):
  PYTHONPATH=.:augur/x/pm_reifier python3 augur/x/pm_reifier/backtest_llama.py
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request

os.environ.setdefault("ZAI_API_KEY", "ollama-local")  # satisfy run_spike import; no z.ai call is made

import kernel_percentile
from backtest import build_series, jsd_to_uniform, m_index
from kernel import SERIES

MODEL = "llama3.1:8b"  # documented training cutoff Dec 2023
T0 = "2023-12"  # anchor at the cutoff; everything after is leakage-free
N_HIST = 12  # shorter history than the z.ai runs — CPU prompt-processing is slow
MAX_STEPS = 18  # cap the window (CPU: ~1-2 min/call, sequential — ollama NUM_PARALLEL=1)
OLLAMA = "http://127.0.0.1:11434/api/chat"
TRANSCRIPTS = pathlib.Path(__file__).parent / "transcripts"


def ollama_call(endpoint: str, body: dict, tag: str) -> dict:
    payload = {
        "model": body["model"],
        "messages": body["messages"],
        "stream": False,
        "format": "json",  # ollama constrains output to valid JSON
        "options": {"temperature": body.get("temperature", 1.0), "num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    TRANSCRIPTS.mkdir(exist_ok=True)
    (TRANSCRIPTS / f"{tag}.json").write_text(json.dumps({"request": payload, "response": d}, indent=2) + "\n")
    content = d["message"]["content"]
    usage = {"total_tokens": d.get("prompt_eval_count", 0) + d.get("eval_count", 0)}
    return {"choices": [{"message": {"content": content}}], "usage": usage}


kernel_percentile._call = ollama_call  # route the kernel at the local model


def main() -> None:
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)
    targets = []
    for tgt in common:
        ti = m_index(tgt)
        if ti <= m_index(T0):
            continue
        hist_months = [m for m in common if m_index(m) < ti][-N_HIST:]
        if len(hist_months) == N_HIST and all(tgt in vals[s] for s in SERIES):
            targets.append((tgt, hist_months))
    targets = targets[:MAX_STEPS]
    print(
        f"{MODEL} leakage-free percentile backtest: anchor {T0}, {len(targets)} steps "
        f"({targets[0][0]}..{targets[-1][0]}), N_HIST={N_HIST}"
    )

    by_series: dict[str, list[float]] = {s: [] for s in SERIES}
    escapes = 0
    escape_n = 0
    steps = []
    for tgt, hist_months in targets:
        history = [(m, {s: vals[s][m] for s in SERIES}) for m in hist_months]
        quantiles, usage = kernel_percentile.sample_step(OLLAMA, MODEL, history, tgt, 1.0, f"llama_{tgt}")
        pits = {}
        for s in SERIES:
            if s in quantiles:
                pits[s] = kernel_percentile.pit(quantiles[s], vals[s][tgt])
                by_series[s].append(pits[s])
                lo, hi = quantiles[s][min(quantiles[s])], quantiles[s][max(quantiles[s])]
                escape_n += 1
                escapes += int(vals[s][tgt] < lo or vals[s][tgt] > hi)
        steps.append({"month": tgt, "pits": pits, "n_series": len(pits), "tokens": usage["total_tokens"]})
        print(f"  {tgt}: {len(pits)}/5 series parsed, tokens={usage['total_tokens']}")

    pooled = [p for ps in by_series.values() for p in ps]
    tail = sum(1 for u in pooled if u <= 0.1 or u >= 0.9) / len(pooled) if pooled else float("nan")
    summary = {
        "model": MODEL,
        "kernel": "percentile",
        "anchor": T0,
        "leakage_free": True,
        "steps": len(steps),
        "n_pits": len(pooled),
        "mean_pit": sum(pooled) / len(pooled) if pooled else None,
        "tail_escape": tail,
        "p1_p99_escape_rate": escapes / escape_n if escape_n else None,
        "jsd_pooled": jsd_to_uniform(pooled),
        "n_by_series": {s: len(ps) for s, ps in by_series.items()},
        "per_step": steps,
    }
    (pathlib.Path(__file__).parent / "results" / "backtest_llama.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n=== {MODEL} (leakage-free, anchor {T0}): {len(steps)} steps, {len(pooled)} PITs ===")
    print(
        f"  mean PIT {summary['mean_pit']:.3f}  tail-escape {tail:.0%}  p1/p99 escape "
        f"{summary['p1_p99_escape_rate']:.0%}  JSD {summary['jsd_pooled']:.3f}"
    )


if __name__ == "__main__":
    main()
