# Sandbox Policy Profiles (Draft)

A menu of profiles to assemble into seatbelt policies. Use as checklists when composing/iterating policies. Assumptions with the explicit policy model:

- JUPYTER_CONFIG_DIR/DATA_DIR are set via policy env to run_root paths; PATH points to control venv bin first
- Kernels are sandboxed via a generated kernelspec that runs under seatbelt; no default_kernel_name traits are set
- CWD = workspace; HOME = run_root (recommended); Jupyter server runs unsandboxed on 127.0.0.1
- Python bytecode: set PYTHONPYCACHEPREFIX=<run_root>/pycache or PYTHONDONTWRITEBYTECODE=1
- No seatbelt macros (no WORKSPACE/RUN_ROOT params); wrapper generates allow rules from policy read_paths/write_paths

Legend

- R/W = read/write allowed; R/O = read-only
- FS = filesystem; NET = networking; ENV = environment variables

---

## 1) safest

- FS write: ONLY workspace, run_root (and optional /tmp if needed)
- FS read: venv site-packages, git repo (read-only), .git (read-only), system libs (/System, /usr, /lib), workspace (read)
- NET: allow loopback; egress allowed (TBD). Consider loopback-only by default
- ENV: minimal allowlist (e.g., PATH, LANG, PYTHONPATH, PYTHONPYCACHEPREFIX). Avoid secrets; prefer explicit env_passthrough entries only when needed
- Process/IPC: minimal (`process*`, signal self, basic ipc, sysctl-read)
- Devices: /dev/null, /dev/urandom, /dev/random; no TTY writes
- Tracing: On by default in dev; off in prod
- Notes: Strictest default for code execution on local repos

Checklist

- [ ] `file*(subpath WORKSPACE)`
- [ ] `file*(subpath RUN_ROOT)`
- [ ] `file-read*` curated system roots + venv + repo + .git
- [ ] network: loopback only (or off by default)
- [ ] env: HOME=RUN_ROOT, strip tokens
- [ ] Python .pyc → RUN_ROOT/pycache or disabled
- [ ] Data viz (matplotlib/plotnine):
  - Set MPLCONFIGDIR=<RUN_ROOT>/mpl (wrapper already does)
  - Optional: set MPLBACKEND=Agg (non-interactive)
  - Allow read of font directories: /System/Library/Fonts, /Library/Fonts (macOS), and fontconfig files
  - Optional: XDG_CACHE_HOME=<RUN_ROOT>/cache to catch font caches

---

## 2) safest+openai

- As “safest”, plus:
- NET: allow egress only to OpenAI endpoints (via proxy or direct allowlist)
- ENV: allowlist OPENAI_API_KEY (optionally OAI proxy envs)

Checklist

- [ ] network-outbound via proxy or domain/IP allowlist for openai.com
- [ ] env allow OPENAI_API_KEY (and only that)

---

## 3) research-online

- FS: same as “safest”
- NET: allow arbitrary egress
- ENV: minimal allowlist (no secrets unless explicitly passed per-run)

Checklist

- [ ] network-outbound allow all
- [ ] env stripped

---

## 4) offline

- FS: same as “safest”
- NET: deny all outbound; allow loopback only
- ENV: minimal

Checklist

- [ ] deny network-outbound
- [ ] allow network-inbound (local ip) optional

---

## 5) workspace-only

- FS write: ONLY WORKSPACE
- FS read: ONLY WORKSPACE + venv + essential system libs
- NET: loopback only
- ENV: minimal

Checklist

- [ ] remove `RUN_ROOT/file*` writes except runtime
- [ ] no /tmp writes (PYTHONPYCACHEPREFIX→RUN_ROOT/pycache)

---

## 6) dev-local

- FS write: WORKSPACE, RUN_ROOT, /tmp (for speed/tools)
- FS read: repo, venv, system libs
- NET: loopback + optional egress
- ENV: allow `PYTHON*`, PATH, LANG; still strip secrets by default

Checklist

- [ ] /tmp writes enabled
- [ ] allow external tooling (e.g., pip) if required (optional)

---

## 7) loopback-only

- FS: like “safest”
- NET: outbound only to 127.0.0.1; deny everything else
- ENV: minimal

Checklist

- [ ] network-outbound restricted to loopback

---

## 8) proxy-egress

- FS: like “safest”
- NET: outbound only via local HTTP proxy (127.0.0.1:PORT); deny direct egress
- ENV: allowlist proxy env if needed (HTTP_PROXY/HTTPS_PROXY)

Checklist

- [ ] network rules enforce proxy path
- [ ] env allowlist proxy vars

---

## 9) read-only sources (+bytecode redirect)

- FS write: WORKSPACE, RUN_ROOT
- FS read: repo + venv (read-only)
- Python: PYTHONPYCACHEPREFIX=<RUN_ROOT>/pycache to avoid writes next to .py

Checklist

- [ ] ensure pycache redirect
- [ ] confirm imports/reloads work

---

## 10) unsandbox (debug only)

- No seatbelt; Jupyter + kernel run unsandboxed
- For diagnostics and development only

Checklist

- [ ] clear flag guarding this mode

---

## Common policy blocks (seatbelt)

- File
  - (allow file-read*(subpath ...)) only for curated roots (no global file-read*)
  - `(allow file* (subpath "<workspace>"))` generated from write_paths
  - `(allow file* (subpath "<run_root>"))` generated from write_paths
  - `(allow file* (subpath "/tmp"))` (optional)
- Net
  - (allow network-inbound (local ip))
  - (allow network-outbound) → restrict/deny per profile (TODO: configurable in wrapper via `net`)
- System
  - `(allow process*)` `(allow signal (target self))`
  - `(allow ipc-posix-*)` `(allow ipc-sysv-shm)` `(allow mach-lookup)` `(allow system-socket)` `(allow sysctl-read)`
  - (allow file*(literal "/dev/null")) (allow file-read* (literal "/dev/urandom"))
- Trace
  - (trace "<RUN_ROOT>/profile.sb") in dev

---

## Environment policy (wrapper)

- HOME=RUN_ROOT, CWD=WORKSPACE
- Strip by default: `**TOKEN`, `**SECRET`, `AWS**`, `GCP**`, `AZURE**`, `SSH**`, etc.
- Allow explicitly per profile: OPENAI_API_KEY, proxy vars
- Set PYTHONPYCACHEPREFIX=<RUN_ROOT>/pycache or PYTHONDONTWRITEBYTECODE=1

---

## Notes

- Jupyter config locking is mandatory; otherwise global kernelspecs may be selected
- Keep manual-first debugging in tmux; encode only after green manual runs
- Tests: use absolute paths in denial checks and robust read-until polling for cold starts
