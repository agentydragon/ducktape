# Model comparison notebook

Plots comparing the wyrm2-runnable models against each other and the closed
frontier, mixing **measured** local results with **third-party** eval numbers.

- `model_comparison.py` — the notebook source (jupytext percent format; the
  diffable source of truth).
- `model_comparison.ipynb` — executed notebook (paired with the `.py`).
- `fig*.png` — the rendered plots.

## What's plotted

1. Decode tokens/s we measured (E1–E5) — decode speed tracks active params.
2. Context we can actually serve — ~256K ceiling; 1M is kernel-blocked.
3. SWE-bench Verified — local models vs Claude/GPT/Gemini anchors.
4. Speed × SWE-bench — our runnable models placed against the frontier line.
5. GPQA / AIME / LiveCodeBench where models report them (sparse).

## Data provenance

- **`local`** numbers (tok/s, context, VRAM) are measured on 2×5090; see
  <../runs/> (E1–E5).
- **`ext`** eval numbers are pulled from vendor cards and independent
  leaderboards (July 2026); each traces to a URL in the notebook's `SOURCES`.
- **Caveat:** SWE-bench Verified mixes vendor self-reports (scaffold-dependent)
  with independent leaderboards (vals.ai) — **not** strictly comparable; read the
  trend, not the decimal. Some newest-closed-model figures had conflicting web
  reports and are omitted rather than guessed.

## Re-render

```bash
LIB=$(dirname "$(find /nix/store -name libstdc++.so.6 -path '*gcc*' | head -1)")
LD_LIBRARY_PATH="$LIB:$LD_LIBRARY_PATH" uv run \
  --with jupytext --with nbconvert --with matplotlib --with numpy --with ipykernel \
  bash -c 'jupytext --to notebook model_comparison.py &&
           jupyter nbconvert --to notebook --execute --inplace model_comparison.ipynb &&
           jupyter nbconvert --to html model_comparison.ipynb'
```

(The `LD_LIBRARY_PATH` shim is the NixOS fix for manylinux wheels — `zmq` needs
`libstdc++.so.6`.)
