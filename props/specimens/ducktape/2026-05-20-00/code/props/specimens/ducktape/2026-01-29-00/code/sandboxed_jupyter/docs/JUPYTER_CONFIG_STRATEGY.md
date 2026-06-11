# Jupyter Config Strategy (Sandboxed Kernel)

We force Jupyter to only see and use our kernelspecs by constraining config and data paths and setting defaults.

```
Environment (set via explicit policy env):
  - JUPYTER_RUNTIME_DIR = <run_root>/runtime
  - JUPYTER_DATA_DIR = <run_root>/data
  - JUPYTER_CONFIG_DIR = <run_root>/config
  - JUPYTER_PATH = <run_root>/data  (limits data search path)

Config file: <RUN_ROOT>/config/jupyter_server_config.py
  - KernelSpecManager.kernel_dirs = ["<run_root>/data/kernels"]
  - KernelSpecManager.ensure_native_kernel = False
  - ServerApp.open_browser = False, ip=127.0.0.1, disable_check_xsrf=True
  - Note: we avoid default_kernel_name traits to reduce notebook_shim conflicts

Kernelspec override:
  - <run_root>/data/kernels/python3/kernel.json wraps sandbox-exec to enforce the seatbelt policy
  - Any doc with default "python3" uses the sandboxed kernel; no macro params (WORKSPACE/RUN_ROOT) are used in policy generation
```

## Python bytecode caches

If sources/venv are mounted read-only, configure Python bytecode handling to avoid writes next to .py files:

- Prefer setting PYTHONPYCACHEPREFIX=<RUN_ROOT>/pycache (redirects `__pycache__` writes)
- Or set PYTHONDONTWRITEBYTECODE=1 to disable .pyc writes entirely (slightly slower imports)

This locks kernel selection to our provided spec, avoiding global/user kernels.
