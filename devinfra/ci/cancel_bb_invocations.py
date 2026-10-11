"""Cancel known child builds as well as the outer bb remote runner on CI cancellation.

Runs with the Actions runner's system Python; keep this stdlib-only. Requests run
concurrently with a short timeout to fit GitHub's cancellation grace period.
"""

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen


def cancel(invocation_id: str) -> bool:
    request = Request(
        "https://app.buildbuddy.io/rpc/BuildBuddyService/CancelExecutions",
        data=json.dumps({"invocationId": invocation_id}).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
    )
    try:
        with urlopen(request, timeout=4):
            pass
    except OSError:
        print(f"bb-remote: cancellation request failed for {invocation_id}", file=sys.stderr)
        return False
    print(f"bb-remote: cancellation requested for {invocation_id}", file=sys.stderr)
    return True


def main() -> None:
    invocation_ids = [str(uuid.UUID(value)) for value in sys.argv[1].split(",") if value]
    if not invocation_ids:
        return
    with ThreadPoolExecutor(max_workers=len(invocation_ids)) as executor:
        succeeded = list(executor.map(cancel, invocation_ids))
    sys.exit(0 if all(succeeded) else 1)


if __name__ == "__main__":
    main()
