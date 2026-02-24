from __future__ import annotations

import importlib
import json
import os
import runpy
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _runtime_dir() -> Path:
    rd = os.environ.get("JUPYTER_RUNTIME_DIR") or os.environ.get("TMPDIR") or os.environ.get("HOME") or "."
    p = Path(rd)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _log_path(name: str = "kernel_boot.log") -> Path:
    return _runtime_dir() / name


def log(msg: str) -> None:
    lp = _log_path()
    with lp.open("a", encoding="utf-8") as f:
        ts = datetime.now(UTC).isoformat(timespec="seconds") + "Z"
        f.write(f"[{ts}] {msg}\n")


def main() -> int:
    try:
        log("shim: starting kernel_shim")
        # Log basic environment for venv/API mismatch debugging
        info = {
            "sys.executable": sys.executable,
            "sys.version": sys.version,
            "argv": sys.argv,
            "env_subset": {
                k: os.environ.get(k)
                for k in ["PATH", "PYTHONPATH", "VIRTUAL_ENV", "JUPYTER_RUNTIME_DIR", "HOME", "TMPDIR"]
            },
        }
        log("shim: info=" + json.dumps(info, indent=2, default=str))
        # Also log first few entries of sys.path
        log("shim: sys.path head=" + json.dumps(sys.path[:10]))
        # Try importing critical packages
        for mod in ("ipykernel", "jupyter_client", "zmq"):
            importlib.import_module(mod)
            log(f"shim: import {mod} OK")

        log("shim: launching ipykernel_launcher")
        # Rewrite argv to mimic -m ipykernel_launcher execution
        sys.argv = [sys.executable, "-m", "ipykernel_launcher", *sys.argv[1:]]
        runpy.run_module("ipykernel_launcher", run_name="__main__")
        return 0
    except SystemExit as e:
        log(f"shim: SystemExit code={e.code}")
        raise
    except Exception:
        log("shim: unhandled exception:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
