import pytest
import pytest_bazel

from haku.console.x.codex_app_server.capture import Sanitizer, SanitizingCapture
from haku.console.x.codex_app_server.protocol import Direction


@pytest.fixture
def short_prompt_sanitizer() -> Sanitizer:
    return Sanitizer(workspace="/private/workspace", prompt="hi", environment_values={})


def test_sanitizer_never_serializes_credentials_environment_paths_or_native_ids():
    sanitizer = Sanitizer(
        workspace="/private/workspace",
        prompt="say the fixed phrase",
        environment_values={"CAPTURE_SECRET": "environment-secret-value"},
    )
    sanitized = sanitizer.sanitize(
        {
            "authorization": "Bearer credential",
            "cookie": "session=credential-cookie",
            "github_token": "plain-token-value",
            "cwd": "/private/workspace",
            "params": {
                "threadId": "019-native-thread",
                "turnId": "019-native-turn",
                "itemId": "native-item",
                "delta": (
                    "say the fixed phrase environment-secret-value /home/person/file sk-secretvalue123456 "
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345 "
                    "https://example.invalid/path?X-Amz-Signature=query-secret"
                ),
            },
        }
    )

    encoded = repr(sanitized)
    assert "credential" not in encoded
    assert "plain-token-value" not in encoded
    assert "query-secret" not in encoded
    assert "signature12345" not in encoded
    assert "environment-secret-value" not in encoded
    assert "/private/workspace" not in encoded
    assert "/home/person" not in encoded
    assert "native-thread" not in encoded
    assert "native-turn" not in encoded
    assert "native-item" not in encoded
    assert sanitized["params"]["threadId"] == "<THREAD_1>"
    assert sanitized["params"]["turnId"] == "<TURN_1>"
    assert sanitized["params"]["itemId"] == "<ITEM_1>"
    assert "<PROMPT>" in sanitized["params"]["delta"]


def test_short_prompt_leaves_unrelated_text_intact(short_prompt_sanitizer):
    # Regression for #4757: the prompt "hi" must not eat the "hi" inside "high" and "which".
    message = "We're currently experiencing high demand, which may cause temporary errors."
    sanitized = short_prompt_sanitizer.sanitize(
        {"method": "error", "params": {"error": {"message": message}, "willRetry": False}}
    )

    assert sanitized["params"]["error"]["message"] == message


def test_short_prompt_is_replaced_at_prompt_bearing_paths(short_prompt_sanitizer):
    turn_start = short_prompt_sanitizer.sanitize(
        {
            "id": "request-1",
            "method": "turn/start",
            "params": {"threadId": "019-native-thread", "input": [{"type": "text", "text": "hi", "text_elements": []}]},
        }
    )
    item_completed = short_prompt_sanitizer.sanitize(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "native-item",
                    "type": "userMessage",
                    "clientId": None,
                    "content": [{"type": "text", "text": "hi", "text_elements": []}],
                },
                "turnId": "019-native-turn",
                "threadId": "019-native-thread",
            },
        }
    )

    assert turn_start["params"]["input"] == [{"type": "text", "text": "<PROMPT>", "text_elements": []}]
    assert item_completed["params"]["item"]["content"] == [{"type": "text", "text": "<PROMPT>", "text_elements": []}]


def test_prompt_is_replaced_in_user_message_items_inside_turn_payloads(short_prompt_sanitizer):
    # Turn.items may carry the userMessage item itself (itemsView "loaded"), not only item/* frames.
    sanitized = short_prompt_sanitizer.sanitize(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "019-native-turn",
                    "error": None,
                    "items": [
                        {
                            "id": "native-item",
                            "type": "userMessage",
                            "clientId": None,
                            "content": [{"type": "text", "text": "hi", "text_elements": []}],
                        }
                    ],
                    "itemsView": "loaded",
                    "status": "completed",
                },
                "threadId": "019-native-thread",
            },
        }
    )

    assert sanitized["params"]["turn"]["items"][0]["content"] == [
        {"type": "text", "text": "<PROMPT>", "text_elements": []}
    ]


def test_workspace_shorter_than_floor_is_refused():
    with pytest.raises(ValueError, match=r"workspace '/w' is shorter than 12 characters"):
        Sanitizer(workspace="/w", prompt="prompt", environment_values={})


def test_environment_value_shorter_than_floor_is_refused_naming_only_the_variable():
    with pytest.raises(ValueError, match=r"environment value SHORT_TOKEN is shorter than 12 characters") as excinfo:
        Sanitizer(workspace="/private/workspace", prompt="prompt", environment_values={"SHORT_TOKEN": "abc123"})

    assert "abc123" not in str(excinfo.value)


def test_from_process_excludes_environment_values_below_the_floor(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPTURE_TEST_SHORT", "abc")
    monkeypatch.setenv("CAPTURE_TEST_SECRET", "capture-test-secret-value")

    sanitizer = Sanitizer.from_process(workspace=tmp_path / "capture-workspace", prompt="hi")

    assert "CAPTURE_TEST_SHORT" not in sanitizer.environment_values
    assert sanitizer.environment_values["CAPTURE_TEST_SECRET"] == "capture-test-secret-value"


def test_capture_refuses_to_write_past_the_total_byte_budget(tmp_path):
    output = tmp_path / "capture.jsonl"
    output.write_text("")
    capture = SanitizingCapture(
        output=output,
        sanitizer=Sanitizer(workspace="/private/workspace", prompt="prompt", environment_values={}),
        max_messages=10,
        max_bytes=20,
    )

    with pytest.raises(RuntimeError, match=r"capture exceeded --max-bytes=20"):
        capture._record(Direction.SERVER_TO_CLIENT, {"method": "notification", "params": {}})

    assert output.read_text() == ""
    assert capture.messages == 0
    assert capture.bytes_written == 0


if __name__ == "__main__":
    pytest_bazel.main()
