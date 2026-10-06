"""A persisted Codex rollout sample for tests of `runner.codex_history`.

Captured from codex-cli 0.152.0 (app-server, persisted thread) over the
`agentplane/harness_tests/codex` fake Responses server: the model answers with reasoning and an
`exec_command` call, then with reasoning and a message. Each record is verbatim; only the records the
reader looks at are kept (`response_item`), bracketed by the turn's `event_msg` start and end. The
native app-server process tests use the current Bazel binary pin from `MODULE.bazel`.
"""

import json
from pathlib import Path
from typing import Any

THREAD_ID = "01a10adc-1a47-7192-83ae-eae1b6e370ca"
FIRST_REASONING_ID = "rs_test_25"
SECOND_REASONING_ID = "rs_test_28"
CALL_ID = "call_cap_1"
MESSAGE_ID = "msg_test_29"

ROLLOUT: list[dict[str, Any]] = [
    {
        "timestamp": "2026-10-05T06:59:19.272Z",
        "ordinal": 1,
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "turn_id": "01a10adc-1a55-7b43-aea1-44274729021e",
            "started_at": 1791183559,
            "model_context_window": 258400,
            "collaboration_mode_kind": "default",
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.321Z",
        "ordinal": 4,
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": "msg_01a10adc-1a99-7d41-b273-816a70941973",
            "role": "user",
            "content": [{"type": "input_text", "text": "Capture the rollout"}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "01a10adc-1a55-7b43-aea1-44274729021e",
                "create_time": 1791183559.3218431,
                "content_item_kinds": ["user.text"],
            },
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.357Z",
        "ordinal": 7,
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "id": "rs_test_25",
            "summary": [{"type": "summary_text", "text": "first thought"}],
            "encrypted_content": "enc_cap_1",
            "internal_chat_message_metadata_passthrough": {"turn_id": "01a10adc-1a55-7b43-aea1-44274729021e"},
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.361Z",
        "ordinal": 8,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "id": "fc_test_26",
            "name": "exec_command",
            "arguments": '{"cmd": "printf \'CAP\\\\n\'"}',
            "call_id": "call_cap_1",
            "internal_chat_message_metadata_passthrough": {"turn_id": "01a10adc-1a55-7b43-aea1-44274729021e"},
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.466Z",
        "ordinal": 10,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "id": "fco_01a10adc-1b2a-7df0-8ed7-9c27a07f058d",
            "call_id": "call_cap_1",
            "output": "Chunk ID: 1a0253\n"
            "Wall time: 0.0001 seconds\n"
            "Process exited with code 0\n"
            "Original token count: 1\n"
            "Output:\n"
            "CAP\n",
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "01a10adc-1a55-7b43-aea1-44274729021e",
                "create_time": 1791183559.4662817,
            },
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.516Z",
        "ordinal": 13,
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "id": "rs_test_28",
            "summary": [{"type": "summary_text", "text": "second thought"}],
            "encrypted_content": "enc_cap_2",
            "internal_chat_message_metadata_passthrough": {"turn_id": "01a10adc-1a55-7b43-aea1-44274729021e"},
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.521Z",
        "ordinal": 15,
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": "msg_test_29",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "CAP_DONE"}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "01a10adc-1a55-7b43-aea1-44274729021e",
                "content_item_kinds": ["unknown"],
            },
        },
    },
    {
        "timestamp": "2026-10-05T06:59:19.535Z",
        "ordinal": 17,
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "01a10adc-1a55-7b43-aea1-44274729021e",
            "last_agent_message": "CAP_DONE",
            "started_at": 1791183559,
            "completed_at": 1791183559,
            "duration_ms": 277,
            "time_to_first_token_ms": 96,
        },
    },
]


def write_rollout(codex_home: Path, records: list[dict[str, Any]]) -> None:
    path = codex_home / "sessions" / "2026" / "10" / "05" / f"rollout-2026-10-05T06-59-19-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
