"""Throwaway visualization: the real recent-history tail + a bunch of LLM rollout lines.

Reconstructs each world's macro path from the committed windowed transcripts (transcripts/conv*_w0_win*),
prepends the real recent-history tail (real_history.json), and plots history (bold) + every rollout
(thin) per series, plus an OpenAI valuation step panel from the emitted events.

matplotlib is not in the repo env; run with uv-managed deps (the NixOS system python lacks pip and a
working venv):
  uv run --no-project --python 3.12 --with matplotlib python augur/x/pm_reifier/plot_rollouts.py
"""

from __future__ import annotations

import datetime
import json
import pathlib

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")  # headless render to PNG

HERE = pathlib.Path(__file__).parent
REAL = json.loads((HERE / "real_history.json").read_text())
OPENAI = json.loads((HERE / "openai_history.json").read_text())
HISTORY = REAL["series"]
LEVEL_SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
ANCHOR_VAL_B = OPENAI["anchor_valuation_usd_b"]
HM = len(HISTORY["sp500"])  # history months
HORIZON = 60
AS_OF = datetime.date.fromisoformat(f"{REAL['as_of']}-01")


def month_label(m: int) -> str:
    total = AS_OF.month + m
    return datetime.date(AS_OF.year + (total - 1) // 12, (total - 1) % 12 + 1, 1).strftime("%b'%y")


def load_worlds() -> list[dict]:
    worlds = []
    for ci in range(16):
        base = HERE / "transcripts" / f"conv{ci:02d}_w0"
        paths: dict[str, list[float]] = {s: [HISTORY[s][-1]] for s in LEVEL_SERIES}
        events: list[dict] = []
        ok = True
        for t in range(5):
            f = base.parent / f"{base.name}_win{t}_a0.json"
            if not f.exists():
                ok = False
                break
            d = json.loads(f.read_text())["response"]
            if "choices" not in d:
                ok = False
                break
            obj = json.loads(d["choices"][0]["message"]["content"])
            m = obj.get("months", {})
            if any(not isinstance(m.get(s), list) or len(m[s]) != 12 for s in LEVEL_SERIES):
                ok = False
                break
            for s in LEVEL_SERIES:
                paths[s].extend(float(x) for x in m[s])
            events.extend(e for e in obj.get("openai_events", []) if isinstance(e, dict) and "month" in e)
        if ok:
            worlds.append({"paths": paths, "events": sorted(events, key=lambda e: e["month"])})
    return worlds


def oai_step(events: list[dict]) -> tuple[list[int], list[float]]:
    xs, ys = [0], [float(ANCHOR_VAL_B)]
    for e in events:
        if isinstance(e.get("valuation_usd_b"), int | float):
            xs.append(int(e["month"]))
            ys.append(float(e["valuation_usd_b"]))
    xs.append(HORIZON)
    ys.append(ys[-1])
    return xs, ys


def main() -> None:
    worlds = load_worlds()
    panels = [*LEVEL_SERIES, "openai_valuation"]
    titles = {
        "inflation": "CPI index (=100 now)",
        "sp500": "S&P 500",
        "crypto:BTC": "BTC (USD)",
        "home_value:sf_ca": "SF home-price index (=100)",
        "rent:sf_ca": "SF rent index (=100)",
        "openai_valuation": "OpenAI valuation ($B, event step)",
    }
    fig, axes = plt.subplots(3, 2, figsize=(14, 11))
    cmap = plt.get_cmap("turbo")
    colors = [cmap(i / max(1, len(worlds) - 1)) for i in range(len(worlds))]
    hist_x = list(range(-(HM - 1), 1))  # months -(HM-1)..0
    roll_x = list(range(HORIZON + 1))

    for ax, key in zip(axes.flat, panels, strict=True):
        if key == "openai_valuation":
            for w, c in zip(worlds, colors, strict=True):
                xs, ys = oai_step(w["events"])
                ax.step(xs, ys, where="post", color=c, alpha=0.55, lw=1.0)
                for e in w["events"]:
                    if e.get("kind") == "ipo":
                        ax.scatter([e["month"]], [e.get("valuation_usd_b", 0)], color=c, marker="^", s=40, zorder=5)
            ax.scatter([0], [ANCHOR_VAL_B], color="k", zorder=6, label="now ($852B)")
            ax.axhline(2000, ls=":", color="gray", lw=1)
            ax.set_ylabel("$B")
        else:
            for w, c in zip(worlds, colors, strict=True):
                ax.plot(roll_x, w["paths"][key], color=c, alpha=0.5, lw=0.9)
            ax.plot(hist_x, HISTORY[key], color="k", lw=2.6, label="real recent history")
        ax.axvline(0, color="k", ls="--", lw=0.8, alpha=0.6)
        ax.set_title(titles[key], fontsize=11)
        ax.set_xlim(-(HM - 1), HORIZON)
        ax.set_xticks(list(range(0, HORIZON + 1, 12)))
        ax.set_xticklabels([month_label(m) for m in range(0, HORIZON + 1, 12)])
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8, loc="upper left")

    fig.suptitle(
        f"augur PM-reifier — LLM rollouts from {AS_OF.strftime('%B %Y')}  "
        f"({len(worlds)} worlds, glm-4.7; black = real recent history, △ = OpenAI IPO event)",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out = HERE / "results" / "rollouts.png"
    fig.savefig(out, dpi=120)
    print(f"wrote {out} from {len(worlds)} worlds")


if __name__ == "__main__":
    main()
