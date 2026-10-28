"""Run extracted native modules with synthetic startup inputs and a pinned Bun runtime."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from devinfra.js.bun_standalone_extract import parse_modules
from util.bazel.runfiles import get_required_path


def run(script: Path, *, workspace: Path, target: Path, artifacts: Path) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="claude-native-modules-") as temporary:
        root = Path(temporary)
        binary = get_required_path("claude_remote_tools_cli_linux_x64/claude").read_bytes()
        provenance: dict[str, object] = {"binary_sha256": hashlib.sha256(binary).hexdigest(), "runtime": "Bun 1.3.14"}
        # Diagnostic logging only: preserve native startup decisions and exception handling.
        logging_taps = {
            "chunk-6b4khzmf.js": (
                b"let B=j().catch((h)=>{",
                b'let B=j().catch((h)=>{console.error("NATIVE_START_ERROR",h.stack);',
            ),
            "chunk-etbngzss.js": (
                b"function c(t){let e=W(t);",
                b'function c(t){console.error("NATIVE_ERROR",t?.stack??t);let e=W(t);',
            ),
            "chunk-bd805sh6.js": (
                b'function t(e,n={level:"debug"}){',
                b'function t(e,n={level:"debug"}){console.error("NATIVE_DEBUG",e);',
            ),
        }
        patches = []
        for module in parse_modules(binary):
            destination = root / module["name"].removeprefix("/$bunfs/root/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            contents = module["contents"]
            if destination.suffix == ".js" or destination.name == "cli":
                contents = contents.replace(b"/$bunfs/root/", str(root).encode() + b"/")
            if destination.name in logging_taps:
                before, after = logging_taps[destination.name]
                if contents.count(before) != 1:
                    raise ValueError(f"Expected one logging insertion point in {destination.name}")
                contents = contents.replace(before, after)
                patches.append(
                    {
                        "module": destination.name,
                        "original_sha256": hashlib.sha256(module["contents"]).hexdigest(),
                        "logging_patch_applied": True,
                    }
                )
            destination.write_bytes(contents)
        if len(patches) != len(logging_taps):
            raise ValueError("Pinned executable is missing expected logging modules")
        provenance["logging_patches"] = patches
        provenance["source_relocation"] = "/$bunfs/root/ imports rewritten to extracted temporary directory"
        (artifacts / "source-provenance.json").write_text(json.dumps(provenance, indent=2))
        entry = root / "probe.mjs"
        entry.write_text(script.read_text().replace("NATIVE_ROOT", json.dumps(str(root))))
        return subprocess.run(
            [str(get_required_path("claude_remote_tools_bun/bun")), str(entry)],
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "CLAUDE_CONFIG_DIR": str(root / "config"),
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1,localhost",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "PROBE_TARGET": str(target),
            },
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
