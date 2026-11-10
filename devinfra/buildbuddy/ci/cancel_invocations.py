"""Cancel the invocation IDs supplied by this CI run within its teardown grace period."""

import subprocess
import sys
import uuid


def main() -> None:
    invocation_ids = [str(uuid.UUID(value)) for value in sys.argv[1].split(",") if value]
    if not invocation_ids:
        return
    result = subprocess.run(
        ["bbapi", "--timeout", "4s", "invocation", "cancel", *invocation_ids], check=False, timeout=6
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
