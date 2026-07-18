# Model comparison notebook

Plots comparing the wyrm2-runnable models against each other and the closed
frontier, mixing **measured** local results with **third-party** eval numbers.

- `model_comparison.py` — the single source (jupytext percent format; diffable).
- `fig*.svg` — the rendered plots (committed so they show in the repo/PR view).
- The `.ipynb` and `.html` are **generated** from the `.py` and are gitignored —
  run the re-render below to produce them.

## What's plotted

1. Decode tokens/s we measured (E1–E5) — decode speed tracks active params.
2. Context we can actually serve — ~256K ceiling; 1M is kernel-blocked.
3. Speed × SWE-bench — runnable models placed against the frontier reference lines,
   each point labelled with the setting its SWE number was measured at.
4. GPQA / AIME / LiveCodeBench — source-verified, no-tools, local models only.
5. gpt-oss AIME across the effort × tools dial — why a single eval score is
   meaningless without the setting.

## Data provenance

- **`local`** numbers (tok/s, context, VRAM) are measured on 2×5090; see
  <../runs/> (E1–E8).
- **`ext`** eval numbers trace to a URL in the notebook's `SOURCES`, and each is
  **pinned to the reasoning-effort / tools setting its source states** (in the bar
  labels).
- **Comparability:** GPQA/AIME are held at no-tools/high (verified from the cards);
  frontier anchors whose linked pages don't state a no-tools number are omitted from
  those, not guessed. SWE-bench can't be held to one setting across sources (gpt-oss
  high-effort ceiling vs Sonnet's 10-trial vs vals.ai harness) — read tiers, not
  decimals.

## Re-render

```bash
LD_LIBRARY_PATH="$(nix eval --raw nixpkgs#stdenv.cc.cc.lib)/lib" \
  uv run --with jupytext,nbconvert,matplotlib,numpy,ipykernel,nbformat \
  bash -c 'jupytext --to notebook model_comparison.py -o model_comparison.ipynb &&
           jupyter nbconvert --execute --to html --no-input model_comparison.ipynb'
```

Produces `fig*.svg` + `model_comparison.html` (code cells hidden). The
`LD_LIBRARY_PATH` bit is the NixOS fix for uv's manylinux wheels — `zmq` needs
`libstdc++.so.6`; `nix eval` resolves the current `gcc-lib` (no hardcoded store
path).
