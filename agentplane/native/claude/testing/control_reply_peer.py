"""Pipe peer that interleaves unrelated frames before the facade's control reply."""

import json
import sys


def main() -> None:
    request = json.loads(sys.stdin.readline())
    frames = [
        {"type": "system", "subtype": "task_notification", "task_id": "child", "status": "stopped"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "num_turns": 0,
            "origin": {"kind": "task-notification"},
        },
        {"type": "result", "subtype": "error_during_execution", "is_error": True},
        {"type": "control_response", "response": {"subtype": "success", "request_id": "another-request"}},
    ]
    if sys.argv[1] != "eof":
        frames.append(
            {
                "type": "control_response",
                "response": {"subtype": sys.argv[1], "request_id": request["request_id"], "error": "probe rejection"},
            }
        )
    for frame in frames:
        print(json.dumps(frame), flush=True)


if __name__ == "__main__":
    main()
