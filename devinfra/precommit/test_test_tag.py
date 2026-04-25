from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest
import pytest_bazel

from devinfra.precommit.test_tag import (
    BuildBuddyInvocation,
    Invocations,
    LocalInvocation,
    NoTests,
    TestTagError,
    check_commit_message,
    is_exempt,
    parse_test_tag,
    verify_invocations_on_buildbuddy,
)

_UUID_STR = "abc12345-1234-5678-9abc-def012345678"
_UUID_STR_2 = "11111111-2222-3333-4444-555555555555"
_UUID = uuid.UUID(_UUID_STR)
_UUID_2 = uuid.UUID(_UUID_STR_2)


class TestIsExempt:
    @pytest.mark.parametrize(
        "message", ["Merge branch 'feature' into main", "fixup! Add new feature", "squash! Add new feature"]
    )
    def test_exempt(self, message):
        assert is_exempt(message)

    def test_normal_commit(self):
        assert not is_exempt("Add new feature")


class TestParseTestTag:
    @pytest.mark.parametrize(
        ("tag_value", "expected"),
        [
            (f"buildbuddy:{_UUID_STR}", Invocations([BuildBuddyInvocation(_UUID)])),
            (f"local:{_UUID_STR}", Invocations([LocalInvocation(_UUID)])),
            (
                f"buildbuddy:{_UUID_STR},local:{_UUID_STR_2}",
                Invocations([BuildBuddyInvocation(_UUID), LocalInvocation(_UUID_2)]),
            ),
            ("none: docs only", NoTests("docs only")),
        ],
    )
    def test_valid(self, tag_value, expected):
        assert parse_test_tag(f"Title\n\nBAZEL_TEST_INVOCATIONS={tag_value}") == expected

    def test_in_middle_of_body(self):
        msg = f"Title\n\nSome context.\nBAZEL_TEST_INVOCATIONS=buildbuddy:{_UUID_STR}\nMore text."
        assert parse_test_tag(msg) == Invocations([BuildBuddyInvocation(_UUID)])

    @pytest.mark.parametrize(
        ("tag_value", "match"),
        [
            ("", "empty"),
            ("none:", "explanation"),
            ("none:   ", "explanation"),
            (_UUID_STR, "Invalid invocation reference"),
            (f"gitlab:{_UUID_STR}", "Unknown invocation source"),
            ("buildbuddy:not-a-uuid", "Invalid UUID"),
        ],
    )
    def test_invalid(self, tag_value, match):
        with pytest.raises(TestTagError, match=match):
            parse_test_tag(f"Title\n\nBAZEL_TEST_INVOCATIONS={tag_value}")

    def test_missing(self):
        with pytest.raises(TestTagError):
            parse_test_tag("Add feature\n\nSome body")


class TestCheckCommitMessage:
    @pytest.mark.parametrize(
        "message",
        [
            f"Add feature\n\nBAZEL_TEST_INVOCATIONS=buildbuddy:{_UUID_STR}",
            f"Add feature\n\nBAZEL_TEST_INVOCATIONS=local:{_UUID_STR}",
            "Fix typo\n\nBAZEL_TEST_INVOCATIONS=none: docs only",
            "Merge branch 'feature' into main",
            "fixup! Add feature",
        ],
    )
    def test_passes(self, message):
        check_commit_message(message)

    @pytest.mark.parametrize("message", ["Add feature\n\nSome body", "Fix typo\n\nBAZEL_TEST_INVOCATIONS=none:"])
    def test_raises(self, message):
        with pytest.raises(TestTagError):
            check_commit_message(message)


class TestVerifyInvocations:
    def test_valid_test_invocation(self, monkeypatch):
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        mock_response = httpx.Response(200, json={"invocation": [{"invocationId": _UUID_STR, "command": "test"}]})
        with patch("httpx.post", return_value=mock_response):
            verify_invocations_on_buildbuddy([_UUID])

    def test_build_invocation_rejected(self, monkeypatch):
        """A 'build' invocation with no children is rejected."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        mock_response = httpx.Response(200, json={"invocation": [{"invocationId": _UUID_STR, "command": "build"}]})
        with (
            patch("httpx.post", return_value=mock_response),
            pytest.raises(TestTagError, match="'build' invocation, not 'test'"),
        ):
            verify_invocations_on_buildbuddy([_UUID])

    def test_wrapper_invocation_resolves_to_child(self, monkeypatch):
        """A 'remote test' wrapper invocation is accepted if its child is a 'test'."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        child_id = "22222222-3333-4444-5555-666666666666"
        wrapper_response = httpx.Response(
            200,
            json={
                "invocation": [
                    {
                        "invocationId": _UUID_STR,
                        "command": "remote test",
                        "event": [
                            {"buildEvent": {"children": [{"childInvocationCompleted": {"invocationId": child_id}}]}}
                        ],
                    }
                ]
            },
        )
        child_response = httpx.Response(200, json={"invocation": [{"invocationId": child_id, "command": "test"}]})

        def mock_post(url, *, json, **kwargs):
            inv_id = json["lookup"]["invocationId"]
            return wrapper_response if inv_id == _UUID_STR else child_response

        with patch("httpx.post", side_effect=mock_post):
            verify_invocations_on_buildbuddy([_UUID])

    def test_wrapper_with_non_test_child_rejected(self, monkeypatch):
        """A wrapper invocation whose children are all non-test is rejected."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        child_id = "22222222-3333-4444-5555-666666666666"
        wrapper_response = httpx.Response(
            200,
            json={
                "invocation": [
                    {
                        "invocationId": _UUID_STR,
                        "command": "remote build",
                        "event": [
                            {"buildEvent": {"children": [{"childInvocationCompleted": {"invocationId": child_id}}]}}
                        ],
                    }
                ]
            },
        )
        child_response = httpx.Response(200, json={"invocation": [{"invocationId": child_id, "command": "build"}]})

        def mock_post(url, *, json, **kwargs):
            inv_id = json["lookup"]["invocationId"]
            return wrapper_response if inv_id == _UUID_STR else child_response

        with (
            patch("httpx.post", side_effect=mock_post),
            pytest.raises(TestTagError, match=r"wrapper.*none.*child.*'test'"),
        ):
            verify_invocations_on_buildbuddy([_UUID])

    def test_unknown_invocation_raises(self, monkeypatch):
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        mock_response = httpx.Response(200, json={"invocation": []})
        with patch("httpx.post", return_value=mock_response), pytest.raises(TestTagError, match="not found"):
            verify_invocations_on_buildbuddy([_UUID])

    def test_no_api_key_skips(self, monkeypatch):
        monkeypatch.delenv("BUILDBUDDY_API_KEY", raising=False)
        verify_invocations_on_buildbuddy([_UUID])

    def test_network_error_raises(self, monkeypatch):
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        with (
            patch("httpx.post", side_effect=httpx.ConnectError("connection refused")),
            pytest.raises(TestTagError, match="connection refused"),
        ):
            verify_invocations_on_buildbuddy([_UUID])

    def test_retriable_status_exhausts_retries(self, monkeypatch):
        """Persistent retriable HTTP errors (500/502/503) exhaust all retries and raise TestTagError."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="internal server error")

        with patch("httpx.post", side_effect=mock_post), patch("time.sleep"), pytest.raises(TestTagError, match="500"):
            verify_invocations_on_buildbuddy([_UUID])
        assert call_count == 5

    def test_retries_on_502_then_succeeds(self, monkeypatch):
        """Transient 502 errors are retried; success on a subsequent attempt is accepted."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        success_response = httpx.Response(200, json={"invocation": [{"invocationId": _UUID_STR, "command": "test"}]})
        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(502)
            return success_response

        with patch("httpx.post", side_effect=mock_post), patch("time.sleep"):
            verify_invocations_on_buildbuddy([_UUID])
        assert call_count == 3

    def test_retries_on_503_exhausted(self, monkeypatch):
        """Persistent 503 errors exhaust all 5 retries and raise TestTagError."""
        monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return httpx.Response(503)

        with patch("httpx.post", side_effect=mock_post), patch("time.sleep"), pytest.raises(TestTagError, match="503"):
            verify_invocations_on_buildbuddy([_UUID])
        assert call_count == 5


if __name__ == "__main__":
    pytest_bazel.main()
