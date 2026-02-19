# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2.0"]
# ///
"""Capture live container version information for comparison across updates.

Collects runtime versions, package versions, environment-manager metadata,
environment variables, and key binary hashes into a structured YAML artifact.

Usage:
    uv run --script tools/capture_versions.py > versions.yaml
    uv run --script tools/capture_versions.py --diff previous-versions.yaml

The output is deterministic and diffable. Future sessions can compare against
a previous capture to identify what changed in the container image.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: str, *, timeout: int = 10) -> str:
    """Run a shell command and return stripped stdout, or empty string on failure."""
    try:
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def version_line(cmd: str) -> str:
    """Run a version command and return the first line."""
    out = run(cmd)
    return out.split("\n")[0] if out else ""


def sha256_file(path: str) -> str:
    """Return SHA256 hex digest of a file, or empty string if missing."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, PermissionError):
        return ""


def collect_node_versions() -> dict[str, str]:
    """Collect Node.js versions from /opt/node*."""
    versions = {}
    for d in sorted(Path("/opt").glob("node*")):
        if d.is_dir() and (d / "bin" / "node").exists():
            v = run(f"{d / 'bin' / 'node'} --version")
            if v:
                versions[d.name] = v
    return versions


def collect_npm_globals() -> dict[str, str]:
    """Collect globally installed npm package versions."""
    packages: dict[str, str] = {}
    node22_modules = Path("/opt/node22/lib/node_modules")
    if not node22_modules.exists():
        return packages
    for pkg_dir in sorted(node22_modules.iterdir()):
        pjson = pkg_dir / "package.json"
        if pkg_dir.name.startswith("@"):
            # Scoped package
            for sub in sorted(pkg_dir.iterdir()):
                pjson = sub / "package.json"
                if pjson.exists():
                    try:
                        data = json.loads(pjson.read_text())
                        packages[f"{pkg_dir.name}/{sub.name}"] = data.get("version", "?")
                    except (json.JSONDecodeError, OSError):
                        pass
        elif pjson.exists():
            try:
                data = json.loads(pjson.read_text())
                packages[pkg_dir.name] = data.get("version", "?")
            except (json.JSONDecodeError, OSError):
                pass
    return packages


def collect_python_versions() -> dict[str, str]:
    """Collect Python interpreter versions."""
    versions = {}
    for v in ["3.10", "3.11", "3.12", "3.13"]:
        out = run(f"python{v} --version 2>&1")
        if out:
            versions[f"python{v}"] = out
    return versions


def collect_ruby_versions() -> dict[str, str]:
    """Collect Ruby versions from /opt/ruby-*."""
    versions = {}
    for d in sorted(Path("/opt").glob("ruby-*")):
        if d.is_dir() and (d / "bin" / "ruby").exists():
            v = run(f"{d / 'bin' / 'ruby'} --version")
            if v:
                versions[d.name] = v.split()[1] if len(v.split()) > 1 else v
    return versions


def collect_go_versions() -> dict[str, str]:
    """Collect Go versions from /usr/local/go*."""
    versions = {}
    for d in sorted(Path("/usr/local").glob("go*")):
        if d.is_dir() and (d / "bin" / "go").exists():
            v = run(f"{d / 'bin' / 'go'} version")
            if v:
                # "go version go1.24.7 linux/amd64" -> "go1.24.7"
                parts = v.split()
                versions[d.name] = parts[2] if len(parts) >= 3 else v
    return versions


def collect_env_vars() -> dict[str, str]:
    """Collect Claude-specific environment variables, excluding session-specific ones."""
    redact_prefixes = ("sk-ant-", "eyJ", "ghs_", "ghp_")
    # These vary per session and shouldn't be in the versioned artifact
    session_specific_keys = {
        "CLAUDE_CODE_CONTAINER_ID",
        "CLAUDE_CODE_DIAGNOSTICS_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_REMOTE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
        "CODESIGN_MCP_PORT",
        "CODESIGN_MCP_TOKEN",
    }
    result = {}
    for key in sorted(os.environ):
        if key in session_specific_keys:
            continue
        if key.startswith(("CLAUDE", "CODESIGN", "MCP_", "CLAUDECODE")):
            val = os.environ[key]
            if any(val.startswith(p) for p in redact_prefixes):
                val = val[:10] + "...<redacted>"
            result[key] = val
    return result


def collect_environment_manager() -> dict[str, str]:
    """Collect environment-manager metadata."""
    info: dict[str, str] = {}
    info["version"] = version_line("/usr/local/bin/environment-manager --version 2>&1")
    info["sha256"] = sha256_file("/usr/local/bin/environment-manager")

    # Collect subcommands
    help_out = run("/usr/local/bin/environment-manager --help 2>&1")
    commands = []
    in_commands = False
    for line in help_out.split("\n"):
        if "Available Commands:" in line:
            in_commands = True
            continue
        if in_commands:
            if line.strip() == "" or line.startswith("Flags:"):
                break
            parts = line.strip().split(None, 1)
            if parts:
                commands.append(parts[0])
    info["subcommands"] = ", ".join(commands)

    # Sandbox settings
    sandbox = run("/usr/local/bin/environment-manager print-sandbox-settings 2>&1")
    if sandbox:
        try:
            parsed = json.loads(sandbox)
            info["enableWeakerNestedSandbox"] = str(parsed.get("enableWeakerNestedSandbox", "?"))
        except json.JSONDecodeError:
            pass

    return info


def collect_dpkg_packages() -> dict[str, str]:
    """Collect all installed dpkg package versions."""
    out = run("dpkg-query -W -f='${Package} ${Version}\n'", timeout=30)
    if not out:
        return {}
    packages = {}
    for line in out.split("\n"):
        parts = line.split(None, 1)
        if len(parts) == 2:
            packages[parts[0]] = parts[1]
    return packages


def collect_binary_hashes() -> dict[str, str]:
    """Collect SHA256 hashes of key binaries."""
    binaries = [
        "/process_api",
        "/usr/local/bin/environment-manager",
        "/usr/local/bin/golangci-lint",
        "/usr/local/bin/composer",
        "/usr/local/bin/check-tools",
    ]
    return {b: sha256_file(b) for b in binaries if Path(b).exists()}


def yaml_dump(data: dict, indent: int = 0) -> str:
    """Simple YAML serializer (no external deps beyond stdlib)."""
    lines = []
    prefix = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(yaml_dump(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            for item in value:
                lines.append(f"{prefix}  - {item}")
        else:
            # Quote strings with special chars
            display = value
            if isinstance(display, str) and any(c in display for c in ":#{}[]|>&*!%@"):
                display = f'"{display}"'
            lines.append(f"{prefix}{key}: {display}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--diff":
        if len(sys.argv) < 3:
            print("Usage: capture_versions.py --diff <previous-versions.yaml>", file=sys.stderr)
            sys.exit(1)
        diff_mode(sys.argv[2])
        return

    captured = datetime.datetime.now(datetime.UTC).isoformat()

    data: dict[str, object] = {}

    # OS
    data["os"] = {
        "release": version_line("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'"),
        "kernel": run("uname -r"),
        "arch": run("uname -m"),
    }

    # Hardware
    data["hardware"] = {
        "cpus": run("nproc"),
        "memory": run("free -h | awk '/^Mem:/ {print $2}'"),
        "disk": run("df -h / | awk 'NR==2 {print $2}'"),
    }

    # Environment manager
    data["environment_manager"] = collect_environment_manager()

    # Runtimes
    data["node"] = collect_node_versions()
    data["python"] = collect_python_versions()
    data["ruby"] = collect_ruby_versions()
    data["go"] = collect_go_versions()
    data["rust"] = {"rustc": version_line("rustc --version")}
    data["bun"] = {"version": version_line("bun --version")}
    data["java"] = {"version": version_line("java --version 2>&1")}
    data["php"] = {"version": version_line("php --version | head -1")}

    # Build tools
    data["build_tools"] = {
        "podman": version_line("podman --version"),
        "buildah": version_line("buildah --version"),
        "composer": version_line("composer --version 2>&1"),
        "maven": version_line("mvn --version 2>&1 | head -1"),
        "gradle": version_line("gradle --version 2>&1 | grep '^Gradle'"),
        "golangci-lint": version_line("golangci-lint --version 2>&1"),
        "uv": version_line("uv --version"),
    }

    # dpkg packages
    data["dpkg_packages"] = collect_dpkg_packages()

    # npm globals
    data["npm_globals"] = collect_npm_globals()

    # Claude-specific env vars
    data["claude_env_vars"] = collect_env_vars()

    # Binary hashes
    data["binary_sha256"] = collect_binary_hashes()

    # Claude Code
    data["claude_code"] = {"version": version_line("claude --version"), "path": run("which claude")}

    print("# Claude Code Web Container Versions")
    print(f"# Captured: {captured}")
    print("# Use 'uv run --script tools/capture_versions.py --diff <prev>.yaml' to compare")
    print()
    print(yaml_dump(data))


def diff_mode(previous_path: str) -> None:
    """Compare current versions against a previous capture (line-based diff)."""
    # Capture current to temp file
    current = subprocess.run([sys.executable, __file__], check=False, capture_output=True, text=True)
    if current.returncode != 0:
        print(f"Error capturing current versions: {current.stderr}", file=sys.stderr)
        sys.exit(1)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(current.stdout)
        current_path = f.name

    # Diff
    result = subprocess.run(
        [
            "diff",
            "-u",
            "--label",
            f"previous ({previous_path})",
            "--label",
            "current (live)",
            previous_path,
            current_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    Path(current_path).unlink()

    if result.returncode == 0:
        print("No differences found.")
    else:
        print(result.stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
