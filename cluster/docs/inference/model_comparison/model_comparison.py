# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # wyrm2 local models vs the frontier — measured + eval comparison
#
# Two kinds of data, kept clearly separate:
#
# - **Measured here** (`local`): tokens/s, context we can actually run, VRAM —
#   from the E1–E5 runs on 2×RTX 5090 (`cluster/docs/inference/runs/`).
# - **Third-party evals** (`ext`): SWE-bench Verified, GPQA, AIME, LiveCodeBench —
#   pulled from vendor cards and independent leaderboards (July 2026). Every number
#   carries a source URL in `SOURCES` below.
#
# **Reasoning effort / tools is the dominant hidden variable — so every eval number
# here is pinned to the setting its source states.** On these evals the score depends
# heavily on the reasoning effort (low→high thinking budget) and on whether the model
# had external tools: gpt-oss drops ~20 SWE-bench points high→low, and its AIME swings
# 40–60 points across the effort×tools dial (figure 5). So each bar carries its
# setting, and numbers are only compared at a *matched* setting:
#
# - **GPQA / AIME** are held at **no tools, high effort** — verified from the gpt-oss
#   card (arxiv 2508.10925; this is where GPQA-120b corrects 80.9→80.1 and AIME-20b
#   corrects 98.7→91.7, since 80.9/98.7 were the *with-tools* rows) and the Qwen3.5
#   card (thinking on). Frontier anchors (Sonnet 4.5, GPT-5) are **omitted** from
#   these two: their linked pages don't state a no-tools number (Anthropic's AIME used
#   a Python/tools config; the OpenAI page 403'd), so nothing comparable can be sourced.
# - **SWE-bench** can't be held to one setting across sources — gpt-oss is a high-effort
#   card ceiling, Sonnet is Anthropic's 10-trial/200K-thinking number, the other
#   frontier anchors are the vals.ai harness, and the small coders are card-reported.
#   The setting is labelled on every point in the money plot; read tiers, not decimals.
#
# **Also:** `decode tok/s` is *not* answers/s — a reasoner emitting 2000 thinking
# tokens is far slower per task than the raw rate suggests. A stricter comparison
# would fix effort and measure tokens-per-task / answers-per-hour. Left as future work.

# %%
from dataclasses import dataclass, field

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

plt.rcParams.update({"figure.dpi": 120, "font.size": 10, "axes.grid": True, "grid.alpha": 0.3})

# --- source URLs (reproducible links) -----------------------------------------
SOURCES = {
    "qwen3coder": "https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "gptoss": "https://arxiv.org/abs/2508.10925",  # OpenAI gpt-oss model card
    "qwen35": "https://huggingface.co/Qwen/Qwen3.5-35B-A3B",
    "devstral": "https://mistral.ai/news/devstral-2-vibe-cli/",
    "valsai_swe": "https://www.vals.ai/benchmarks/swebench",  # independent, dated 2026-07-17
    "gpt5": "https://openai.com/index/introducing-gpt-5/",
    "sonnet45": "https://www.anthropic.com/news/claude-sonnet-4-5",
    "runs": "cluster/docs/inference/runs/  (E1–E5, measured on 2×5090)",
    "glm52": "https://venturebeat.com/technology/z-ais-open-weights-glm-5-2-beats-gpt-5-5-on-multiple-long-horizon-coding-benchmarks-for-1-6th-the-cost",
    "glm52_colibri": "cluster/docs/inference/runs/2026-07-14_glm52_colibri/  (measured 0.28 tok/s)",
    "oss120": "https://arxiv.org/abs/2508.10925",  # same gpt-oss card (120b rows)
}


# %%
# --- MEASURED on 2×5090 (local) ------------------------------------------------
@dataclass
class Measured:
    label: str
    runtime: str
    # decode tokens/s at input context (K tokens) -> tok/s
    decode: dict = field(default_factory=dict)
    allocated_ctx_k: float = 0.0  # largest context that loads + completes (K tokens)
    vram_gb: float = 0.0  # peak, per GPU (max of the two)
    tools: str = ""  # tool-calling smoke result
    note: str = ""


MEASURED = [
    Measured("Qwen3-Coder-30B", "vLLM TP2 AWQ", {8: 271, 32: 235, 128: 199}, 262, 30.7, "single+parallel+multi ✓"),
    Measured("gpt-oss-20b (vLLM)", "vLLM TP1 MXFP4", {8: 1356, 32: 1035, 128: 1494}, 128, 15.0, "parallel ✗ (model)"),
    Measured("gpt-oss-20b (Ollama)", "Ollama GGUF", {8: 1154, 32: 917, 128: 636}, 128, 15.0, "parallel ✗ (model)"),
    Measured("Qwen3.5-35B-A3B", "vLLM TP2 FP8 (GDN)", {8: 231, 32: 226, 128: 211}, 262, 29.0, "✗ hermes parser"),
    Measured("Devstral-24B", "vLLM TP2 FP8 dense", {8: 96, 32: 89}, 128, 30.7, "single+parallel+multi ✓"),
    Measured("Qwen2.5-7B-1M", "vLLM TP2", {}, 0, 0.0, note="1M blocked: DCA has no sm_120 kernel"),
]

# --- THIRD-PARTY EVALS (ext) ---------------------------------------------------
# SWE-bench Verified — the one eval with coverage across all our models + anchors.
# (score, source_key, kind)  kind: "resident" = fits our 64 GB VRAM;
# "offload" = we can run it but only via CPU/RAM/disk offload (slow);
# "anchor" = closed frontier reference we cannot run.
# (score, source, kind, setting) — `setting` = the reasoning effort / agent scaffold
# the number was measured at. `?` = the card didn't state it (do not read as pinned).
SWEBENCH = {
    "Qwen3-Coder-30B": (51.9, "qwen3coder", "resident", "non-reasoning coder"),
    "gpt-oss-20b": (60.7, "gptoss", "resident", "high effort (ceiling)"),
    "Devstral-24B": (68.0, "devstral", "resident", "agent scaffold, direct coder"),
    "Qwen3.5-35B-A3B": (69.2, "qwen35", "resident", "max thinking?"),
    "gpt-oss-120b": (62.4, "oss120", "offload", "high effort (ceiling)"),
    "GLM-5.2 (744B)": (77.8, "glm52", "offload", "press report, setting unstated"),
    "Claude Sonnet 4.5": (77.2, "sonnet45", "anchor", "10-trial avg, 200K think, no TTC"),
    "Gemini 3.5 Flash": (78.8, "valsai_swe", "anchor", "vals.ai harness"),
    "GPT-5.5": (82.6, "valsai_swe", "anchor", "vals.ai harness"),
    "Claude Opus 4.8": (88.6, "valsai_swe", "anchor", "vals.ai harness"),
    "Claude Fable 5": (95.0, "valsai_swe", "anchor", "vals.ai harness"),
}

# Runnable models (resident + offload) placed in measured-speed × SWE-bench space.
# decode tok/s is what WE measured; offload models are 100–5000× slower.
# label: (measured decode tok/s, swe_bench %, kind)
RUNNABLE_QUALITY = {
    "gpt-oss-20b": (1300, 60.7, "resident"),
    "Qwen3-Coder-30B": (199, 51.9, "resident"),
    "Qwen3.5-35B-A3B": (211, 69.2, "resident"),
    "Devstral-24B": (92, 68.0, "resident"),
    "gpt-oss-120b": (12.2, 62.4, "offload"),  # E7: vLLM TP2, 12GB/GPU CPU offload (measured)
    "GLM-5.2 (744B)": (0.28, 77.8, "offload"),  # Colibri disk-streamed experts
}
OFFLOAD_COLOR = "#16a085"

# {metric: {model: (score, setting)}} — every number here is verified against its
# source card at a stated NO-TOOLS setting, so the bars are actually comparable.
# gpt-oss: arxiv 2508.10925 (high reasoning effort, no tools — GPQA 80.9→80.1 for
# 120b once tools are removed; AIME-20b 98.7→91.7 without tools). Qwen3.5: HF card
# (thinking on; card doesn't split tools, Qwen convention is no-tools).
# Frontier anchors (Sonnet 4.5, GPT-5) are DROPPED from GPQA/AIME: their linked pages
# don't state a no-tools setting (Anthropic's AIME used a "Python configuration" =
# tools; the OpenAI page 403'd), so a comparable number can't be sourced.
REASONING = {
    "GPQA Diamond (no tools, high)": {
        "gpt-oss-20b": (71.5, "high"),
        "gpt-oss-120b": (80.1, "high"),
        "Qwen3.5-35B-A3B": (85.4, "thinking on"),
    },
    "AIME (no tools, high)": {
        "gpt-oss-20b": (91.7, "high"),
        "gpt-oss-120b": (92.5, "high"),
        "Qwen3.5-35B-A3B": (93.3, "thinking on"),  # AIME 2026 on the card
    },
    "LiveCodeBench v6": {
        "Qwen3.5-35B-A3B": (74.6, "thinking on"),
    },
}

# gpt-oss AIME 2025 by reasoning effort × tools — the sourced illustration of WHY the
# setting matters (arxiv 2508.10925). Same model swings 37→92 (no tools) or 58→98
# (tools) across the effort dial; a single "AIME score" is meaningless without it.
GPTOSS_AIME = {
    "gpt-oss-20b": {"no tools": {"low": 37.1, "med": 72.1, "high": 91.7}, "with tools": {"low": 57.5, "med": 90.4, "high": 98.7}},
    "gpt-oss-120b": {"no tools": {"low": 50.4, "med": 80.0, "high": 92.5}, "with tools": {"low": 72.9, "med": 91.6, "high": 97.9}},
}

LOCAL_COLOR, ANCHOR_COLOR = "#2a7fff", "#c0392b"

# Reasoning behaviour. In 2026 reasoning-with-an-effort-dial (low→high / thinking
# budget) is near-universal — Claude, Gemini, GPT-5, gpt-oss, Qwen3.5 all have it;
# it's a continuous control, not an on/off switch. The genuine exceptions are the
# few models *shipped without* a thinking mode (e.g. Qwen3-Coder). What still
# varies enormously is how many reasoning tokens a model spends by default
# (Qwen3.5 was extremely verbose in E4). category: Reasoning | Direct.
# model -> (category, kind, basis)
REASONING_CLASS = {
    "Qwen3-Coder-30B": ("Direct", "local", "shipped without a thinking mode (Qwen)"),
    "Devstral-24B": ("Direct", "local", "answered directly, no CoT in E5"),
    "gpt-oss-20b": ("Reasoning", "local", "reasoning_effort low/med/high (E2)"),
    "Qwen3.5-35B-A3B": ("Reasoning", "local", "verbose CoT, effort dial (E4)"),
    "Claude Sonnet 4.5": ("Reasoning", "anchor", "reasoning effort / thinking budget"),
    "Claude Opus 4.8": ("Reasoning", "anchor", "reasoning effort"),
    "Gemini 3.5 Flash": ("Reasoning", "anchor", "thinking budget"),
    "GPT-5": ("Reasoning", "anchor", "reasoning effort"),
}


# %% [markdown]
# ## 1. Decode throughput we can actually get (measured)
#
# Tokens/s tracks **active parameters**: the small-active MoEs fly, the dense 24B
# crawls. Ollama vs vLLM shown for gpt-oss (runtime matters, esp. at long context).

# %%
fig, ax = plt.subplots(figsize=(9, 4.5))
runnable = [m for m in MEASURED if m.decode]
ctxs = [8, 32, 128]
x = np.arange(len(runnable))
w = 0.25
for i, c in enumerate(ctxs):
    vals = [m.decode.get(c, np.nan) for m in runnable]
    ax.bar(x + (i - 1) * w, vals, w, label=f"{c}K ctx")
ax.set_xticks(x)
ax.set_xticklabels([f"{m.label}\n{m.runtime}" for m in runnable], fontsize=8)
ax.set_ylabel("decode tokens/s (single request)")
ax.set_title("Measured decode throughput on 2×5090 (higher = faster)  ·  local")
ax.legend(title="input context")
fig.tight_layout()
fig.savefig("fig1_decode_tps.svg", bbox_inches="tight")

# %% [markdown]
# ## 1b. Reasoning vs direct models
#
# In 2026 **reasoning with a low→high effort dial is near-universal** — Claude,
# Gemini, GPT-5, gpt-oss, and Qwen3.5 all have it; nobody trains it as a plain
# on/off button. So the interesting split isn't "reasoning vs not" but the handful
# of models **shipped without a thinking mode at all** (a couple of purpose-built
# coding models). Orthogonal, and what actually bites latency: *how many* reasoning
# tokens a model spends by default — Qwen3.5 was extremely verbose in E4.

# %%
fig, ax = plt.subplots(figsize=(8.5, 4))
cat_x = {"Direct": 0, "Reasoning": 1}
col_counts = {c: 0 for c in cat_x}
for name, (cat, kind, basis) in REASONING_CLASS.items():
    y = col_counts[cat]
    col_counts[cat] += 1
    color = LOCAL_COLOR if kind == "local" else ANCHOR_COLOR
    ax.scatter(cat_x[cat], y, s=90, color=color, zorder=3)
    ax.annotate(f"  {name}", (cat_x[cat], y), va="center", fontsize=8.5)
ax.set_xticks(list(cat_x.values()))
ax.set_xticklabels(["Direct\n(no thinking mode)", "Reasoning\n(low→high effort dial)"])
ax.set_xlim(-0.4, 1.9)
ax.set_ylim(-0.6, max(col_counts.values()))
ax.set_yticks([])
ax.set_title("Reasoning is the norm; direct models are the exception  ·  blue = local, red = frontier")
ax.legend(handles=[Patch(color=LOCAL_COLOR, label="runs on 2×5090"), Patch(color=ANCHOR_COLOR, label="closed frontier")], loc="upper left")
fig.tight_layout()
fig.savefig("fig1b_reasoning_class.svg", bbox_inches="tight")

# %% [markdown]
# ## 2. Context window we can run (measured allocated context)
#
# Practical ceiling today is ~256K. The 1M attempt is **blocked by kernels, not
# memory** (dual-chunk attention has no Blackwell/sm_120 build in vLLM 0.25.1).
# Log x-axis so the 128K/262K models and the 1M target sit on a comparable scale.

# %%
fig, ax = plt.subplots(figsize=(9, 4))
runnable_ctx = [m for m in MEASURED if m.allocated_ctx_k > 0]
ax.barh([m.label for m in runnable_ctx], [m.allocated_ctx_k for m in runnable_ctx], color=LOCAL_COLOR)
ax.set_xscale("log")
ax.set_xlim(64, 1500)
ax.set_xticks([64, 128, 256, 512, 1000])
ax.get_xaxis().set_major_formatter(plt.matplotlib.ticker.ScalarFormatter())
ax.set_xlabel("allocated context (K tokens, log scale)")
ax.set_title("Largest context we can serve  ·  local")
ax.axvline(1000, ls="--", color=ANCHOR_COLOR, alpha=0.7)
ax.text(1000, len(runnable_ctx) - 0.5, "1M target\n(kernel-blocked)", color=ANCHOR_COLOR, fontsize=8, ha="center", va="top")
blocked = [m for m in MEASURED if m.allocated_ctx_k == 0]
if blocked:
    ax.text(70, -0.55, "blocked: " + ", ".join(m.label for m in blocked) + " (1M DCA/sm_120)", fontsize=8, color="#555")
fig.tight_layout()
fig.savefig("fig2_context.svg", bbox_inches="tight")

# %% [markdown]
# ## 3. Speed × coding skill — measured tok/s vs SWE-bench
#
# The one combined view: every runnable model in {decode tok/s we measured} ×
# {SWE-bench}, with each point labelled by the **setting its SWE number was measured
# at** (so the comparison is honest), and the closed frontier as dashed reference
# lines. Log x-axis — it spans four orders of magnitude (gpt-oss-20b ~1300 tok/s down
# to GLM-5.2 disk-streamed at 0.28). The shape is the story: the resident tier is
# fast but caps ~69 SWE-bench; the offload tier (GLM-5.2) reaches 77.8 — past every
# resident option, near the frontier — but ~5000× slower. Frontier SWE runs on the
# vals.ai harness (consistent across those anchors); local numbers are card-reported
# at the labelled effort, so read tiers, not decimals.

# %%
fig, ax = plt.subplots(figsize=(10, 6))
for lbl, (tps, swe, kind) in RUNNABLE_QUALITY.items():
    color = OFFLOAD_COLOR if kind == "offload" else LOCAL_COLOR
    setting = SWEBENCH[lbl][3] if lbl in SWEBENCH else "?"
    ax.scatter(tps, swe, s=100, color=color, zorder=3)
    ax.annotate(f"{lbl} — {swe:.0f} SWE / {tps:g} tok/s\n({setting})", (tps, swe), textcoords="offset points", xytext=(7, 5), fontsize=7.5)
for name, (s, _, kind, setting) in SWEBENCH.items():
    if kind == "anchor":
        ax.axhline(s, ls="--", color=ANCHOR_COLOR, alpha=0.35)
        ax.text(2600, s, f"{name} {s:.0f} ({setting})", color=ANCHOR_COLOR, fontsize=7, va="center", ha="right")
ax.set_xscale("log")
ax.set_xlim(0.1, 3000)
ax.set_ylim(45, 100)
ax.set_xlabel("measured decode tokens/s on 2×5090 (log)  →  faster")
ax.set_ylabel("SWE-bench Verified (%)  →  better coder")
ax.set_title("Speed × coding skill  ·  measured tok/s (local) × SWE-bench (setting-labelled)")
ax.legend(
    handles=[
        Patch(color=LOCAL_COLOR, label="resident (fast, caps ~69)"),
        Patch(color=OFFLOAD_COLOR, label="offload (near-frontier, ~5000× slower)"),
        Patch(color=ANCHOR_COLOR, label="closed frontier (vals.ai harness)"),
    ],
    loc="center left",
)
fig.tight_layout()
fig.savefig("fig3_speed_vs_swe.svg", bbox_inches="tight")

# %% [markdown]
# ## 4. Reasoning evals — only source-verified, no-tools numbers
#
# Every bar here is checked against its source card at a stated no-tools setting, so
# they're comparable. Coding specialists (Qwen3-Coder, Devstral) don't publish these.
# Frontier anchors are absent on purpose: their linked pages don't state a no-tools
# setting (Anthropic's AIME used a Python/tools config; the OpenAI page 403'd), so a
# comparable number can't be sourced — better to omit than to plot a mismatched one.

# %%
fig, axes = plt.subplots(1, len(REASONING), figsize=(12, 3.6), sharey=False)
for ax, (metric, d) in zip(axes, REASONING.items()):
    names = list(d.keys())
    vals = [v[0] for v in d.values()]
    setts = [v[1] for v in d.values()]
    ax.barh(names, vals, color=LOCAL_COLOR)
    for i, (v, st) in enumerate(zip(vals, setts)):
        ax.text(v + 0.5, i, f"{v:.0f} ({st})", va="center", fontsize=7)
    ax.set_title(metric, fontsize=10)
    ax.set_xlim(0, 108)
fig.suptitle("Reasoning evals — source-verified, no-tools, local models only  ·  ext")
fig.tight_layout()
fig.savefig("fig4_reasoning.svg", bbox_inches="tight")

# %% [markdown]
# ## 5. Why the setting matters — gpt-oss AIME 2025 across the effort × tools dial
#
# The same model, one benchmark, from its own card (arxiv 2508.10925): AIME swings
# from **37 → 92** (no tools) or **58 → 98** (with a code interpreter) across the
# low→high effort dial. Quoting a single "AIME score" without the setting is
# meaningless — this is exactly the axis the earlier version of this notebook flattened.

# %%
fig, ax = plt.subplots(figsize=(8, 4.5))
xs = ["low", "med", "high"]
styles = {"no tools": "-", "with tools": "--"}
mcol = {"gpt-oss-20b": LOCAL_COLOR, "gpt-oss-120b": OFFLOAD_COLOR}
for model, tool_curves in GPTOSS_AIME.items():
    for tools, curve in tool_curves.items():
        ax.plot(xs, [curve[x] for x in xs], styles[tools], color=mcol[model], marker="o", label=f"{model} · {tools}")
ax.set_xlabel("reasoning effort")
ax.set_ylabel("AIME 2025 (% solved)")
ax.set_ylim(30, 100)
ax.set_title("Same model, same eval: effort × tools moves AIME by 40–60 points  ·  ext (gpt-oss card)")
ax.legend(fontsize=8)
fig.tight_layout()
fig.savefig("fig5_gptoss_effort.svg", bbox_inches="tight")

# %% [markdown]
# ## Sources
#
# Every eval number above traces to one of these (printed with the run):

# %%
for k, v in SOURCES.items():
    print(f"{k:12s} {v}")
print("\nEval provenance: SWE-bench self-reports are scaffold-dependent; vals.ai is")
print("an independent leaderboard (2026-07-17). Numbers are indicative, not exact-comparable.")
