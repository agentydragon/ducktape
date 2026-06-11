# CLI Design — Minimal Configurable Knobs

Goal: A small, composable set of flags that cover the core axes (FS, NET, ENV), while auto-configuring the rest (pycache/viz). Defaults should be safe.

## Minimal flags

- `--profile <name>`
  - Presets: `safest`, `safest+openai`, `research-online`, `offline`, `dev-local`
  - Sets sensible defaults for FS/NET/ENV (below). CLI overrides profile.

- `--fs-write <roots>`
  - Comma list of write roots: `workspace`, `run-root`, `tmp`
  - Example: `--fs-write workspace,run-root`

- `--net <mode>`
  - `none` | `loopback` | `allowlist:<domains>` | `proxy:<host>:<port>` | `all`

- `--env-allow <vars>`
  - Comma list of env vars to pass through (exact names). Everything else is stripped by default (deny-globs: `*TOKEN,*SECRET,AWS_*,GCP_*,AZURE_*,SSH_*`).
  - Example: `--env-allow OPENAI_API_KEY,HTTP_PROXY,HTTPS_PROXY`

- `--home <mode>`
  - `run-root` (default) | `inherit`

- `--policy-dump`
  - Print effective seatbelt policy to stderr for validation.

Notes:

- We always lock Jupyter to RUN_ROOT (config/data/path), and override `python3` to the sandboxed kernelspec.
- We implicitly redirect Python bytecode to `<RUN_ROOT>/pycache` and set `MPLCONFIGDIR=<RUN_ROOT>/mpl`; no extra flags needed.
- Font paths are granted read by default on macOS: `/System/Library/Fonts`, `/Library/Fonts`.

## Profile defaults

- `safest`
  - `--fs-write workspace,run-root`
  - `--net loopback`
  - `--env-allow PATH,LANG,PYTHONPATH`
  - HOME=RUN_ROOT

- `safest+openai`
  - Inherits `safest` + `--net allowlist:openai.com,api.openai.com` (or `--net proxy:127.0.0.1:7890`)
  - `--env-allow OPENAI_API_KEY`

- `research-online`
  - Inherits `safest` + `--net all`

- `offline`
  - Inherits `safest` + `--net none`

- `dev-local`
  - Inherits `safest` + `--fs-write workspace,run-root,tmp`

## Examples

Safest:

```bash
sandbox-jupyter --workspace /abs/repo --mode seatbelt \
  --profile safest --policy-dump
```

Safest + OpenAI:

```bash
sandbox-jupyter --workspace /abs/repo --mode seatbelt \
  --profile safest+openai --env-allow OPENAI_API_KEY --policy-dump
```

Offline:

```bash
sandbox-jupyter --workspace /abs/repo --mode seatbelt \
  --profile offline --policy-dump
```

Custom:

```bash
sandbox-jupyter --workspace /abs/repo --mode seatbelt \
  --fs-write workspace,run-root --net proxy:127.0.0.1:7890 \
  --env-allow OPENAI_API_KEY,HTTP_PROXY,HTTPS_PROXY --policy-dump
```

## Implementation sketch

- Map minimal flags → seatbelt policy fragments + Jupyter env
- Always set: JUPYTER_CONFIG_DIR/DATA_DIR/PATH to RUN_ROOT; override python3 kernelspec → sandbox-exec
- Auto set: PYTHONPYCACHEPREFIX, MPLCONFIGDIR; grant read of system fonts on macOS
- Deny env by default via a deny-glob list; pass through only `--env-allow` names
- Keep `--policy-dump` for tmux validation
