from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    # Usage:
    #   python -m sandboxed_jupyter.kernel_exec \
    #     --stderr-log /path/to/log -- [kernel args like -f {connection_file}]
    try:
        args = sys.argv[1:]
        log_path: Path | None = None
        if "--" in args:
            dd = args.index("--")
            pre = args[:dd]
            post = args[dd + 1 :]
        else:
            pre = args
            post = []
        it = iter(pre)
        for tok in it:
            if tok == "--stderr-log":
                try:
                    log_path = Path(next(it))
                except StopIteration:
                    print("kernel_exec: --stderr-log requires a path", file=sys.stderr)
                    return 2
            else:
                # ignore unknown flags for forwards-compat
                pass
        if log_path is None:
            print("kernel_exec: missing --stderr-log", file=sys.stderr)
            return 2
        _ensure_parent(log_path)
        # Redirect stderr to the log file (append mode)
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(fd, 2)
            os.close(fd)
        except Exception as e:
            # Last resort: continue without redirect but note it
            print(f"kernel_exec: failed to redirect stderr: {e}", file=sys.stderr)
        # Decide target module based on diagnostics flag
        diag = os.environ.get("SJ_DEBUG_DIAG")
        target_mod = "sandboxed_jupyter.kernel_shim" if diag else "ipykernel_launcher"
        argv = [sys.executable, "-m", target_mod, *post]
        os.execv(sys.executable, argv)
    except Exception:
        # In case redirect succeeded, this goes to the file
        print("kernel_exec: unhandled exception:\n" + traceback.format_exc(), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
