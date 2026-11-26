"""Run Lean for one module and reject the same admitted-goal warning as rules_lean."""

import os
import subprocess
import sys


def main() -> int:
    lean, root, source, olean, c_file = sys.argv[1:]
    env = {**os.environ, "LEAN_PATH": root}
    result = subprocess.run(
        [lean, "--root=" + root, "-o", olean, "-c", c_file, source],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode:
        return result.returncode
    # rules_lean uses this same prefix because Lean exits 0 for admitted proofs.
    if "declaration uses " in result.stdout or "declaration uses " in result.stderr:
        print("Lean module contains an admitted declaration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
