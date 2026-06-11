# TODOs / Notes

- Policy tightening (when ready):
  - Restrict network-outbound to loopback or specific ports (Jupyter server)
  - Make reads explicit-only (done: removed global file-read*) and tighten test fixtures to allow_read_all: false with minimal read_paths
  - Reduce remaining allowances (mach-lookup/system-socket) if not required by kernel
  - Remove `/dev/tty` write if not needed
- WORKSPACE and RUN_ROOT semantics:
  - WORKSPACE: repo/workspace root passed via `--workspace`. Kernel may read/write anywhere under this path
  - RUN_ROOT: ephemeral per-run directory under /tmp (runtime logs, notebook scratch, kernelspec, jupyter runtime)
- Desired final mode:
  - Wrapper runs with `--workspace <repo root>` so kernel has R/W under repo; no write outside WORKSPACE, RUN_ROOT, tmp

## Worktree-level service idea

Concept: run sandboxed Jupyter MCP servers as a per-worktree background service managed by `wt`.

Sketch:

- `wt` allocates a `JP_PORT` and per-worktree `JUPYTER_*` dirs under `.wt/state/jupyter/`
- Starts `sandbox-jupyter --workspace <root> --mode seatbelt --jupyter-port $PORT` with inherited env
- Exposes an mcpServers block or a small shim to register with clients
- Lifecycle: `wt up` / `wt down` manage the server

Not implemented yet. Keep wrapper minimal for now.
