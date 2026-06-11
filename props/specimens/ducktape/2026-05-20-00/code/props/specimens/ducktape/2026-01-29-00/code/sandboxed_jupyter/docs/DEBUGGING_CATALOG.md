# Debugging Catalog — Jupyter MCP STDIO Guard

A practical catalog of manual debugging affordances to use with tmux and shell.
Use these to triage sandbox issues, MCP stdio protocol, kernel behavior, and Jupyter runtime.

## Layout & Setup

- Use the 3-pane layout from TMUX_MANUAL_TESTING.md (Jupyter, MCP, Logs/Driver)
- Environment variables
  - `WS`: workspace (repo root)
  - `PORT`: chosen TCP port
  - `TOKEN`: Jupyter token
  - `PKG_SRC`: path to `src/` for `PYTHONPATH` injection when running the wrapper from source

## MCP stdio poking

- Send newline-delimited JSON directly into the MCP process (Pane 2)
  - `initialize` → wait for result
  - `notifications/initialized` → no result expected
  - `tools/call append_execute_code_cell` → verify content/structuredContent
- Retry patterns
  - If initialize is slow: resend after short wait or increase read timeout
  - Use the driver in `scratch/manual_unsandbox.sh` for quick loops

## Jupyter Server diagnostics

- Logs
  - Tail `$WS/logs/jupyter.out` and `$WS/logs/jupyter.err`
  - In wrapper mode: `<RUN_ROOT>/runtime/jupyter_server.*` under `RUN_ROOT`
- API reachability (token-protected)

  ```bash
  curl -s "http://127.0.0.1:${PORT}/api" -H "Authorization: token ${TOKEN}" | head -c 200
  ```

- Startup readiness
  - socket connect loop to `127.0.0.1:${PORT}`

## Kernelspec verification

- List kernelspecs visible to this Jupyter instance

  ```bash
  JUPYTER_DATA_DIR=<from wrapper stderr> jupyter kernelspec list
  ```

- Inspect sandboxed kernelspec file written by the wrapper

  ```bash
  cat "$RUN_ROOT/data/kernels/python3/kernel.json" | jq .
  ```

  `argv` must include: `sandbox-exec -f <policy> <kernel-python> -m ipykernel_launcher -f {connection_file}`

- Notebook's kernelspec

  ```bash
  jq .metadata.kernelspec < "$WS/.mcp/<path>.ipynb"
  ```

  `name` must be `python3` (the wrapper provides a sandboxed kernelspec for that name)

## Kernel argv / process verification

- Increase Jupyter logging (if needed)
  - Set env: `JUPYTER_LOG_LEVEL=DEBUG` (or use `--debug` flags)
- Check that launched kernel argv contains `sandbox-exec`
  - Look in `jupyter_server.err` for kernel spawn lines
  - Or strace-equivalent is limited on macOS; rely on logs

## Sandbox policy & interactive testing

- Inspect generated policy

  ```bash
  cat "$RUN_ROOT/policy.sb"
  ```

- Enable policy trace (`wrapper --trace-sandbox`)
  - A trace file path is printed in stderr; open it after run to see denials
- Run an interactive shell under the same policy

  ```bash
  sandbox-exec -f "$RUN_ROOT/policy.sb" bash -lc 'pwd; whoami; touch "$WS/.mcp/ok"; touch /etc/denied || echo $?'
  ```

  Try file writes/reads to confirm policy effects

- Direct kernel smoke under sandbox

  ```bash
  sandbox-exec -f "$RUN_ROOT/policy.sb" "$KERNEL_PY" - <<'PY'
  import pathlib, sys
  print("pwd ok")
  try:
      (pathlib.Path("/etc/deny_me")).write_text("x")
      print("wrote /etc — SHOULD NOT HAPPEN")
  except Exception as e:
      print(type(e).__name__, e)
  PY
  ```

## macOS logs (seatbelt denials)

- View recent seatbelt messages

  ```bash
  log show --style syslog --last 5m --predicate 'subsystem == "com.apple.sandbox"'
  log show --style syslog --last 5m --predicate 'process == "sandbox-exec"'
  ```

- Narrow down by timestamp or process ID if available

## Kernel-side probes (via MCP tool)

- Inspect environment and paths from inside the kernel
  - cell_source:

    ```python
    import os, sys, pathlib
    print("CWD:", os.getcwd())
    print("HOME:", os.environ.get("HOME"))
    print("SYS.PATH:", sys.path)
    print("CAN WRITE WS?", (pathlib.Path(".mcp/test.txt").write_text("x"), True))
    ```

- Attempt controlled writes
  - WORKSPACE write (should succeed): `touch WS/.mcp/ok`
  - RUN_ROOT write (should succeed): `touch "$RUN_ROOT"/runtime/ok`
  - Outside write (should fail): touch a sibling outside WORKSPACE

## Network checks

- Loopback only (intended once tightened)

  ```bash
  python - <<'PY'
  import socket; s=socket.socket(); s.connect(("127.0.0.1", PORT)); print("ok")
  PY
  ```

- External egress (if allowed): `curl https://example.com` (should fail when tightened)

## Verbosity & tracing

- Python verbose imports in kernel
  - cell_source: `import sys; sys.flags` or set `PYTHONVERBOSE=1` in kernelspec for a run
- Wrapper debug prints
  - `--trace-sandbox` to print policy paths and enable trace file

## Log aggregation

- Wrapper tee logs
  - `<RUN_ROOT>/mcp_stdout.log` and `mcp_stderr.log` (latest)
- Jupyter logs
  - `$WS/logs/*` or `RUN_ROOT/runtime/jupyter_server.*`
- Save useful tails on failure to `tmp_path` for inspection

## Common failure patterns

- Initialize returns None → increase read timeout or ensure Jupyter port is ready
- Tools/call never returns → kernel failed to launch; check kernelspec usage and seatbelt denials
- Writes succeeding outside WORKSPACE → sandboxed kernelspec not applied; verify kernelspec and notebook metadata, or policy too permissive

## One-liner helpers

- Pick a free port

  ```bash
  python - <<'PY'
  import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()
  PY
  ```

- Generate token

  ```bash
  python - <<'PY'
  import secrets; print(secrets.token_urlsafe(16))
  PY
  ```

## When to tighten policy

- After confirming sandboxed kernel is in use and behavior in manual runs matches expectations (denials appear), reduce allowances:
  - Limit network-outbound to loopback
  - Remove global tmp writes; rely on WORKSPACE and RUN_ROOT only
  - Trim mach-lookup/system-socket to required subset
  - Remove `/dev/tty` writes if not needed
