#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from sandboxed_jupyter.sandboxer import Policy as SandboxPolicy, build_sandboxer_command

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Pydantic config schema (single path, no profiles)
# -----------------------------------------------------------------------------


class KernelConfig(BaseModel):
    name: str = "python3"
    display_name: str = "Python 3 (sandboxed)"
    language: str = "python"
    argv_base: list[str]


class JupyterConfig(BaseModel):
    # Extra python config appended verbatim after defaults
    config_py_extra: str | None = None


class ComposerConfig(BaseModel):
    version: int = 1
    bundle_dir: str
    runtime_dir: str
    kernel: KernelConfig
    jupyter: JupyterConfig | None = None
    policy: SandboxPolicy = Field(default_factory=SandboxPolicy)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _write_default_jupyter_config(config_dir: Path, extra_py: str | None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    default_lines = [
        "c = get_config()",
        "c.KernelSpecManager.ensure_native_kernel = False",
        "c.ServerApp.open_browser = False",
        "c.ServerApp.ip = '127.0.0.1'",
        "c.ServerApp.disable_check_xsrf = True",
        "c.ServerApp.websocket_ping_interval = 30000",
        "c.ServerApp.websocket_ping_timeout = 30000",
    ]
    content = "\n".join(default_lines) + "\n"
    if extra_py:
        content += "\n# --- composer appended config (verbatim) ---\n" + extra_py.rstrip("\n") + "\n"
    (config_dir / "jupyter_server_config.py").write_text(content)


def _ensure_policy_minimums(policy: dict, *, runtime_dir: Path, kernel_exec: str) -> dict:
    env_map = policy.setdefault("env", {})
    env_set = env_map.setdefault("set", {})
    env_map.setdefault("passthrough", [])
    env_set.setdefault("HOME", runtime_dir.as_posix())
    env_set.setdefault("PYTHONPYCACHEPREFIX", (runtime_dir / "pycache").as_posix())
    env_set.setdefault("MPLCONFIGDIR", (runtime_dir / "mpl").as_posix())
    env_set.setdefault("TMPDIR", (runtime_dir / "tmp").as_posix())
    env_set.setdefault("TMP", (runtime_dir / "tmp").as_posix())
    env_set.setdefault("TEMP", (runtime_dir / "tmp").as_posix())

    fs_map = policy.setdefault("fs", {})
    rp = fs_map.setdefault("read_paths", [])
    wp = fs_map.setdefault("write_paths", [])
    # Ensure kernel executable and its venv root are readable
    if kernel_exec and kernel_exec not in rp:
        rp.append(kernel_exec)
    kexec_path = Path(kernel_exec)
    if not kexec_path.is_absolute():
        kexec_path = kexec_path.absolute()  # preserve symlinks, avoid resolve() here
    # Add both the symlink path and the resolved real path (if different)
    exec_symlink = kexec_path.as_posix()
    if exec_symlink not in rp:
        rp.append(exec_symlink)
    try:
        exec_real = kexec_path.resolve(strict=False).as_posix()
    except Exception as e:
        # Fall back to the symlink path if resolution fails; log for diagnostics
        logger.debug("resolve failed for %s: %s", kexec_path, e)
        exec_real = exec_symlink
    if exec_real and exec_real not in rp:
        rp.append(exec_real)
    # Add parent dirs for directory-based allowances (subpath)
    kexec_dir = kexec_path.parent.as_posix()
    if kexec_dir not in rp:
        rp.append(kexec_dir)
    try:
        real_dir = Path(exec_real).parent.as_posix()
        if real_dir not in rp:
            rp.append(real_dir)
    except Exception as e:
        logger.debug("failed to derive real_dir from %s: %s", exec_real, e)
    # Add venv root and lib dirs (site-packages lives here)
    venv_root_path = kexec_path.parents[1]
    venv_root = venv_root_path.as_posix()
    if venv_root not in rp:
        rp.append(venv_root)
    venv_lib = (venv_root_path / "lib").as_posix()
    if venv_lib not in rp:
        rp.append(venv_lib)
    # Best-effort add versioned lib, lib-dynload, and site-packages (common layout)
    for sub in ("python3.12", "python3.12/lib-dynload", "python3.12/site-packages"):
        p = (venv_root_path / "lib" / sub).as_posix()
        if p not in rp:
            rp.append(p)

    platform_map = policy.setdefault("platform", {})
    seatbelt_map = platform_map.setdefault("seatbelt", {})
    extra_allow = seatbelt_map.setdefault("extra_allow", {})
    frx = extra_allow.setdefault("file_read_extra", [])
    # Fonts (matplotlib, etc.)
    for fdir in ["/System/Library/Fonts", "/Library/Fonts"]:
        if fdir not in frx:
            frx.append(fdir)

    if runtime_dir.as_posix() not in wp:
        wp.append(runtime_dir.as_posix())

    net_map = policy.setdefault("net", {})
    net_map.setdefault("mode", "loopback")
    return policy


# -----------------------------------------------------------------------------
# Compose from config
# -----------------------------------------------------------------------------


def compose_from_config_raw(raw_text: str) -> None:
    raw = yaml.safe_load(raw_text)
    try:
        cfg: ComposerConfig = ComposerConfig.model_validate(raw)
    except ValidationError as e:
        raise SystemExit(f"Invalid composer config: {e}") from e

    if cfg.version != 1:
        raise SystemExit(f"unsupported composer config version: {cfg.version}")

    bundle_dir = Path(cfg.bundle_dir).resolve()
    runtime_dir = Path(cfg.runtime_dir).resolve()

    kernel = cfg.kernel
    if not kernel.argv_base:
        raise SystemExit("kernel.argv_base must be a non-empty list")
    kernel_exec = str(kernel.argv_base[0])

    jupyter_cfg = cfg.jupyter or JupyterConfig()

    # Paths (flattened): config lives directly under bundle_dir/config
    config_dir = bundle_dir / "config"
    kernels_dir = bundle_dir / "kernels"
    policy_dir = bundle_dir / "policies"

    for d in (bundle_dir, runtime_dir, config_dir, kernels_dir, policy_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Write Jupyter config (defaults + appended extras)
    _write_default_jupyter_config(config_dir, jupyter_cfg.config_py_extra)

    # Build policy: start from provided model and apply minimal inserts
    policy_node: dict = cfg.policy.model_dump()
    policy_node = _ensure_policy_minimums(policy_node, runtime_dir=runtime_dir, kernel_exec=kernel_exec)

    # Write policy
    policy_path = policy_dir / "policy.yaml"
    with policy_path.open("w") as f:
        yaml.safe_dump(policy_node, f, sort_keys=False)

    # Kernelspec
    kdir = kernels_dir / kernel.name
    kdir.mkdir(parents=True, exist_ok=True)
    kernel_cmd = [*kernel.argv_base, "-f", "{connection_file}"]
    trace = cfg.policy.platform.trace or cfg.policy.platform.seatbelt.trace
    argv = build_sandboxer_command(policy_path, kernel_cmd, trace=trace)
    kernel_json = {"argv": argv, "display_name": kernel.display_name, "language": kernel.language}
    (kdir / "kernel.json").write_text(json.dumps(kernel_json))


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="jupyter-sandbox-compose",
        description="Composer to build a bundle (config/kernels/policies) from a single YAML config",
    )
    ap.add_argument("--config", required=True, help="Path to composer YAML config path or '-' for stdin")
    args = ap.parse_args()

    raw_text = sys.stdin.read() if args.config == "-" else Path(args.config).read_text()
    compose_from_config_raw(raw_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
