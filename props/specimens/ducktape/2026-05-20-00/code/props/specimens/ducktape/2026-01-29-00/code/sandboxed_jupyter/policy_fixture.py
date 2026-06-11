import os
import sys
from pathlib import Path

import yaml


def write_policy(
    ws: Path,
    run_root: Path,
    *,
    allow_read_all: bool | None = None,
    allow_write_all: bool | None = None,
    add_read_paths: list[str] | None = None,
    add_write_paths: list[str] | None = None,
    env_set: dict[str, str] | None = None,
    env_passthrough: list[str] | None = None,
    net: str | None = None,
) -> None:
    for sub in ("runtime", "data", "config", "mpl", "pycache", "tmp"):
        (run_root / sub).mkdir(parents=True, exist_ok=True)

    # Build sandboxer policy (nested schema)
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    kernel_py = Path(sys.executable)
    venv_root_resolved = kernel_py.resolve().parent.parent
    venv_root_symlink = kernel_py.parent.parent

    # IMPORTANT: iterate by the policy (tests/fixtures), not sandboxer code.
    # Narrowed: explicit system + venv paths only; add more via add_read_paths as needed.
    read_paths = [
        str(ws),
        str(run_root),
        # Also allow current working directory to avoid CWD-related startup failures
        str(Path.cwd()),
        # kernel venv symlink and resolved trees
        str(venv_root_symlink),
        str(venv_root_symlink / "bin"),
        str(venv_root_resolved),
        str(venv_root_resolved / "bin"),
        str(venv_root_resolved / "lib"),
        str(venv_root_resolved / "lib" / f"python{ver}"),
        str(venv_root_resolved / "lib" / f"python{ver}" / "site-packages"),
        # minimal system/framework/library locations required by macOS loader and Python
        "/System",
        "/usr/lib",
        "/usr/share",
        "/Library",
        "/etc",
        "/private/etc",
        "/private/var/db",
        "/var/db",
        "/private/var/db/dyld",
        "/System/Volumes/Preboot",
        "/System/Cryptexes",
        "/System/Volumes/Preboot/Cryptexes",
        # Common package manager/system libraries (Homebrew on macOS, Intel/ARM)
        "/opt/homebrew",
        "/usr/local",
        "/bin",
        "/usr/bin",
        "/sbin",
        "/usr/sbin",
        "/usr",
        "/private",
        "/System/Volumes",
        "/dev",
        *(add_read_paths or []),
    ]
    # Default: strict. Only broaden to '/' when explicitly requested.
    if allow_read_all is True:
        read_paths.append("/")
    write_paths = [str(ws), str(run_root), *(add_write_paths or [])]

    control_bin = os.environ.get("SJ_TEST_CONTROL_BIN", "")
    path_prefix = control_bin + (":" if control_bin else "")

    policy = {
        "env": {
            "set": {
                "PATH": path_prefix + os.environ.get("PATH", ""),
                "JUPYTER_RUNTIME_DIR": str(run_root / "runtime"),
                "JUPYTER_DATA_DIR": str(run_root / "data"),
                "JUPYTER_CONFIG_DIR": str(run_root / "config"),
                "JUPYTER_PATH": str(run_root / "data"),
                "MPLCONFIGDIR": str(run_root / "mpl"),
                "PYTHONPYCACHEPREFIX": str(run_root / "pycache"),
                "TMPDIR": str(run_root / "tmp"),
                "TMP": str(run_root / "tmp"),
                "TEMP": str(run_root / "tmp"),
                "PYTHONUNBUFFERED": "1",
                "HOME": str(run_root),
                **({k: str(v) for k, v in (env_set or {}).items()}),
            },
            "passthrough": ["PYTHONPATH", "SJ_DEBUG_DIAG", *(env_passthrough or [])],
        },
        "fs": {"read_paths": read_paths, "write_paths": write_paths},
        "net": {"mode": (net or "loopback")},
        "platform": {
            "trace": False,
            "seatbelt": {
                "extra_allow": {
                    # Allow sysctl queries needed by Python/platform libs
                    "sysctl_read": True,
                    # Start with minimal services (add more if denies show specifics)
                    "mach_lookup": ["com.apple.cfprefsd.agent"],
                    "file_read_extra": [],
                }
            },
        },
    }

    (ws / ".sandbox_jupyter.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
