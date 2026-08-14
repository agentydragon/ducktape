"""Extract the Claude Code executable from the repository-pinned Agent SDK wheel."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1])
    output = Path(sys.argv[2])
    with zipfile.ZipFile(wheel) as archive:
        candidates = [name for name in archive.namelist() if name.endswith("claude_agent_sdk/_bundled/claude")]
        if len(candidates) != 1:
            raise RuntimeError(f"expected one bundled Claude executable, found {candidates}")
        output.write_bytes(archive.read(candidates[0]))
    output.chmod(0o755)


if __name__ == "__main__":
    main()
