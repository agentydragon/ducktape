from __future__ import annotations

import sys
import traceback

from ipykernel.kernelapp import launch_new_instance

from sandboxed_jupyter.kernel_shim import log  # reuse shared logging helper

# Early bootstrap to capture import/startup failures for ipykernel


try:
    # ipykernel_launcher is how Jupyter starts kernels; mirror it but with explicit log
    log("bootstrap: launching ipykernel app")
    # Replicate behavior of ipykernel_launcher: run app.launch_new_instance()
    sys.argv = [sys.executable, "-m", "ipykernel_launcher", *sys.argv[1:]]
    launch_new_instance()
except SystemExit as e:
    # Normal exit path; still record it
    log(f"bootstrap: SystemExit code={e.code}")
    raise
except Exception:
    log("bootstrap: unhandled exception during kernel startup:\n" + traceback.format_exc())
    # Re-raise to preserve behavior; Jupyter will observe kernel crash and restart/log
    raise
