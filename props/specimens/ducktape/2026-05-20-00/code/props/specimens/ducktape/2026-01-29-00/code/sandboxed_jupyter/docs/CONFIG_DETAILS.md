# Configuration Details (explicit-only policy, current behavior)

The wrapper does not make implicit environment or filesystem decisions. All behavior is driven by your policy YAML and required CLI flags.

- No automatic stripping of env vars
- No implicit HOME/MPLCONFIGDIR/TMPDIR/etc
- No auto-injected read or write roots
- No network enforcement yet (field present for future use)

## Required CLI

- stdio subcommand: `sandbox-jupyter stdio`
- Required flags: `--policy-config`, `--workspace`, `--run-root`, `--kernel-python`

## Policy YAML schema

Keys and constraints enforced by the wrapper (pydantic v2):

```yaml
fs:
  read_paths: [] # explicit read allowlist (absolute paths); include '/' for global
  write_paths: [] # explicit write allowlist (absolute paths); include '/' for global

# Environment for child processes (Jupyter server, jupyter-mcp-server, and anything they spawn)
env:
  set: {}
  passthrough: [] # names to import from the parent environment verbatim (e.g., OPENAI_API_KEY)

# Present for future use
net: { mode: loopback }
```

Notes

- Unknown fields in the YAML are rejected (`extra = forbid`).
- Process exec is permitted wherever paths are readable or writeable; narrow by trimming paths.

## Recommended env keys (you provide values)

- JUPYTER_RUNTIME_DIR, JUPYTER_DATA_DIR, JUPYTER_CONFIG_DIR, JUPYTER_PATH
- PYTHONPYCACHEPREFIX (or PYTHONDONTWRITEBYTECODE=1), MPLCONFIGDIR
- HOME (recommended to isolate kernel home to RUN_ROOT)
- PATH (prepend your control venv where `jupyter` and `jupyter-mcp-server` live)

Example

```yaml
env:
  JUPYTER_RUNTIME_DIR: /tmp/sjmcp_run/runtime
  JUPYTER_DATA_DIR: /tmp/sjmcp_run/data
  JUPYTER_CONFIG_DIR: /tmp/sjmcp_run/config
  JUPYTER_PATH: /tmp/sjmcp_run/data
  PYTHONPYCACHEPREFIX: /tmp/sjmcp_run/pycache
  MPLCONFIGDIR: /tmp/sjmcp_run/mpl
  HOME: /tmp/sjmcp_run
  PATH: /abs/control_venv/bin:${PATH}
```

## Jupyter and kernelspec behavior

- Jupyter Server runs unsandboxed on 127.0.0.1. The wrapper writes `<run_root>/config/jupyter_server_config.py` with:
  - `KernelSpecManager.kernel_dirs = ['<run_root>/data/kernels']`
  - `KernelSpecManager.ensure_native_kernel = False`
  - No `default_kernel_name` trait is set (avoids notebook_shim issues)
- The wrapper creates a synthetic notebook if `--document-id` is omitted, with metadata `kernelspec.name = "python3"`.
- The wrapper also writes a sandboxed kernelspec at `<run_root>/data/kernels/python3/kernel.json` whose argv begins with:
  - `sandbox-exec -f <policy.sb> <kernel-python> -m ipykernel_launcher -f {connection_file}`
- Because Jupyter is locked to our kernels directory and native kernels are disabled, the notebook’s kernelspec name ("python3") is sufficient; the default kernel setting is irrelevant.

## Path resolution for tools

- `jupyter-mcp-server` and `jupyter` are resolved using the child environment’s PATH constructed from `env` plus any `env_passthrough`.
- Use a dedicated control venv for these host tools; keep the kernel Python separate if desired.

## Filesystem policy composition

- Base policy denies by default, allows core process/IPC primitives, and currently allows networking (to be tightened later).
- The wrapper appends allow rules from your YAML:
  - Writes: either `(allow file* (subpath "/"))` for `allow_write_all: true`, or one `(allow file* (subpath "..."))` per `write_paths` entry
  - Reads: if `allow_write_all: true` nothing more is needed; else either `(allow file-read* (subpath "/"))` for `allow_read_all: true`, or one `(allow file-read* (subpath "..."))` per `read_paths` entry

Tuning tip

- Start permissive (e.g., include kernel venv `site-packages` and your repo in `read_paths`, and `workspace` + `run_root` in `write_paths`). Then use seatbelt trace (`--trace-sandbox`) to derive the minimal additional reads needed for plotting/fonts, etc.
