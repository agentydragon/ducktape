"""Run Lean and reject its admitted-declaration warning."""

import subprocess
import sys


def main() -> int:
    result = subprocess.run(sys.argv[1:], text=True, capture_output=True, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode:
        return result.returncode
    # Match the existing rules_lean topo compiler's sorry gate. Lean exits zero
    # for an admitted declaration, and other warnings must remain non-fatal.
    if "declaration uses " in result.stdout or "declaration uses " in result.stderr:
        print("Lean module contains an admitted declaration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
