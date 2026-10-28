"""Gmail API client wrapper."""

import base64
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import TypeGuard, cast

from googleapiclient.errors import HttpError
from tenacity import RetryCallState, Retrying, retry_if_result, stop_after_attempt, wait_exponential

from gmail_api.labels import GmailLabel
from gmail_api.service import build_gmail_service
from gmail_archiver.gmail_api_models import (
    CreateFilterRequest,
    GmailFilter,
    GmailMessageMinimal,
    GmailMessageWithHeaders,
)
from gmail_archiver.models import Email

logger = logging.getLogger(__name__)


def _is_rate_limit_error(exception: Exception | None) -> TypeGuard[HttpError]:
    return isinstance(exception, HttpError) and exception.resp.status in (429, 503)


def _get_retry_after(exception: HttpError) -> tuple[int | None, dict]:
    """Extract Retry-After timing from rate limit error.

    Gmail API may provide retry timing in Retry-After header or error message body.
    Returns (seconds_to_wait, debug_info) where seconds_to_wait is None if not found.
    """
    debug_info: dict = {}

    # Capture response details for debugging
    debug_info["status"] = exception.resp.status
    debug_info["headers"] = dict(exception.resp)  # Convert to dict for easier viewing

    # Try to parse response body as JSON
    try:
        debug_info["body"] = json.loads(exception.content.decode("utf-8"))
    except json.JSONDecodeError, UnicodeDecodeError, AttributeError:
        # Fall back to raw string if JSON parsing fails
        try:
            debug_info["body"] = exception.content.decode("utf-8", errors="replace")[:500]
        except AttributeError:
            debug_info["body"] = None

    # Check for Retry-After header (can be seconds or HTTP date)
    retry_after = exception.resp.get("retry-after") or exception.resp.get("Retry-After")
    if retry_after:
        try:
            # Try parsing as integer (seconds)
            return int(retry_after), debug_info
        except ValueError, TypeError:
            pass

    # Check for "Retry after" timestamp in error body
    # Gmail may include: "Retry after 2024-02-26T12:16:36.009Z" in error message
    if isinstance(debug_info.get("body"), dict):
        try:
            # Check in JSON error message field
            error_message = debug_info["body"].get("error", {}).get("message", "")
            if (
                isinstance(error_message, str)
                and "Retry after" in error_message
                and (match := re.search(r"Retry after (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", error_message))
            ):
                # Extract ISO timestamp from error message
                retry_timestamp = datetime.fromisoformat(match.group(1))
                now = datetime.now(UTC)
                seconds_to_wait = max(0, int((retry_timestamp - now).total_seconds()))
                debug_info["retry_timestamp"] = match.group(1)
                return seconds_to_wait, debug_info
        except Exception as e:
            debug_info["parse_error"] = str(e)

    return None, debug_info


@dataclass
class _MessageBatchAttempt[T]:
    results: dict[str, T]
    errors: dict[str, tuple[Exception | None, str]]
    individual_retry_ids: set[str]

    @property
    def rate_limit_errors(self) -> list[HttpError]:
        return [exc for exc, _ in self.errors.values() if _is_rate_limit_error(exc)]


def _wait_for_message_batch(retry_state: RetryCallState) -> float:
    assert retry_state.outcome is not None
    retry_after_values = [
        seconds
        for error in retry_state.outcome.result().rate_limit_errors
        if (seconds := _get_retry_after(error)[0]) is not None
    ]
    if retry_after_values:
        return max(retry_after_values)
    return wait_exponential(multiplier=2, max=60)(retry_state)


def _log_message_batch_retry(retry_state: RetryCallState) -> None:
    assert retry_state.outcome is not None
    assert retry_state.next_action is not None
    retry_results = [_get_retry_after(error) for error in retry_state.outcome.result().rate_limit_errors]

    if retry_state.attempt_number == 1:
        _, first_debug = retry_results[0]
        if first_debug:
            print("\n[i] Rate limit response details:", file=sys.stderr)
            if "status" in first_debug:
                print(f"    Status: {first_debug['status']}", file=sys.stderr)
            if "retry_timestamp" in first_debug:
                print(f"    Retry timestamp: {first_debug['retry_timestamp']}", file=sys.stderr)
            if "body" in first_debug:
                body_str = (
                    json.dumps(first_debug["body"], indent=2)
                    if isinstance(first_debug["body"], dict)
                    else str(first_debug["body"])
                )
                print(f"    Response body: {body_str}", file=sys.stderr)
            print(file=sys.stderr, flush=True)

    wait_time = retry_state.next_action.sleep
    if any(seconds is not None for seconds, _ in retry_results):
        print(
            f"⚠️  Rate limited (attempt {retry_state.attempt_number}), server says retry after {wait_time}s...",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"⚠️  Rate limited (attempt {retry_state.attempt_number}), retrying batch in {wait_time:.1f}s...",
            file=sys.stderr,
            flush=True,
        )


def _return_last_message_batch[T](retry_state: RetryCallState) -> _MessageBatchAttempt[T]:
    assert retry_state.outcome is not None
    return cast(_MessageBatchAttempt[T], retry_state.outcome.result())


class GmailClient:
    """Wrapper around Gmail API for email archival operations.

    TODO: Add a `modify_labels_batch` method that uses batchModify for bulk label
    operations (add/remove multiple labels at once). Callers currently call batchModify
    on `service` directly, and ad-hoc scripts loop through messages individually, which
    is slow - batchModify can process up to 1000 messages per call.
    """

    def __init__(self, token_file: Path):
        self.token_file = token_file
        self.service = self._build_service()
        self._label_cache: dict[str, str] | None = None  # name -> id

    def _build_service(self):
        return build_gmail_service(self.token_file)

    def list_messages_by_query(self, query: str, max_results: int | None = None) -> list[str]:
        return self._list_messages({"q": query}, max_results)

    def _list_messages(self, extra_params: dict, max_results: int | None = None) -> list[str]:
        message_ids: list[str] = []
        page_token = None

        while True:
            query_params = {"userId": "me", "maxResults": 100, **extra_params}
            if page_token:
                query_params["pageToken"] = page_token

            results = self.service.users().messages().list(**query_params).execute()
            message_ids.extend(msg["id"] for msg in results.get("messages", []))

            if max_results and len(message_ids) >= max_results:
                return message_ids[:max_results]

            if not (page_token := results.get("nextPageToken")):
                break

        return message_ids

    def get_messages_metadata_batch(
        self, message_ids: list[str], batch_size: int = 50
    ) -> tuple[list[GmailMessageWithHeaders], list[tuple[str, str]]]:
        """Fetch message metadata (headers only) using batch requests.

        Uses format=metadata to get headers without body content.
        Returns successful GmailMessageWithHeaders objects and per-message failures.
        """
        attempt = self._get_messages_batched(
            message_ids, batch_size, message_format="metadata", parse_response=GmailMessageWithHeaders.model_validate
        )
        return (
            [attempt.results[msg_id] for msg_id in message_ids if msg_id in attempt.results],
            [(msg_id, attempt.errors[msg_id][1]) for msg_id in message_ids if msg_id in attempt.errors],
        )

    def get_messages_raw_batch(
        self, message_ids: list[str], batch_size: int, *, retry_failures: bool = True
    ) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
        """Fetch raw message bytes using batch requests.

        Returns (successful_results, failed_results) where each is a list of (message_id, data) tuples.
        """
        attempt = self._get_messages_batched(
            message_ids,
            batch_size,
            message_format="raw",
            parse_response=lambda response: base64.urlsafe_b64decode(response["raw"]),
        )

        # Retry failed messages individually (only for non-rate-limit errors).
        if retry_failures and attempt.individual_retry_ids:
            print(
                f"Retrying {len(attempt.individual_retry_ids)} failed messages individually (non-rate-limit errors)...",
                file=sys.stderr,
            )

            for msg_id in message_ids:
                if msg_id not in attempt.individual_retry_ids:
                    continue
                try:
                    msg = self.service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
                    raw_bytes = base64.urlsafe_b64decode(msg["raw"])
                    attempt.results[msg_id] = raw_bytes
                    attempt.errors.pop(msg_id, None)
                except Exception as e:
                    error_msg = f"Retry failed: {e!s}"
                    print(f"  Failed retry for {msg_id}: {error_msg[:100]}", file=sys.stderr)
                    attempt.errors[msg_id] = (None, error_msg)

        return (
            [(msg_id, attempt.results[msg_id]) for msg_id in message_ids if msg_id in attempt.results],
            [(msg_id, attempt.errors[msg_id][1]) for msg_id in message_ids if msg_id in attempt.errors],
        )

    def get_messages_batch(self, message_ids: list[str], batch_size: int) -> tuple[list[Email], list[tuple[str, str]]]:
        attempt = self._get_messages_batched(
            message_ids,
            batch_size,
            message_format="raw",
            parse_response=lambda response: Email.from_gmail_response(
                base64.urlsafe_b64decode(response["raw"]), GmailMessageMinimal.model_validate(response)
            ),
        )
        return (
            [attempt.results[msg_id] for msg_id in message_ids if msg_id in attempt.results],
            [(msg_id, attempt.errors[msg_id][1]) for msg_id in message_ids if msg_id in attempt.errors],
        )

    def _get_messages_batched[T](
        self, message_ids: list[str], batch_size: int, *, message_format: str, parse_response: Callable[[dict], T]
    ) -> _MessageBatchAttempt[T]:
        results: dict[str, T] = {}
        errors: dict[str, tuple[Exception | None, str]] = {}
        individual_retry_ids: set[str] = set()
        batches = iter(batched(message_ids, batch_size, strict=False))

        for batch in batches:
            pending_ids = list(batch)

            def fetch_pending_batch() -> _MessageBatchAttempt[T]:
                nonlocal pending_ids
                current_attempt = self._get_message_batch(pending_ids, message_format, parse_response)

                for msg_id, response in current_attempt.results.items():
                    results[msg_id] = response
                    errors.pop(msg_id, None)
                    individual_retry_ids.discard(msg_id)
                for msg_id, error in current_attempt.errors.items():
                    errors[msg_id] = error
                    if msg_id in current_attempt.individual_retry_ids:
                        individual_retry_ids.add(msg_id)
                pending_ids = [
                    msg_id
                    for msg_id, (exception, _) in current_attempt.errors.items()
                    if _is_rate_limit_error(exception)
                ]
                return current_attempt

            batch_attempt = Retrying(
                retry=retry_if_result(lambda attempt: bool(attempt.rate_limit_errors)),
                stop=stop_after_attempt(10),
                wait=_wait_for_message_batch,
                before_sleep=_log_message_batch_retry,
                retry_error_callback=_return_last_message_batch,
            )(fetch_pending_batch)

            if batch_attempt.rate_limit_errors:
                for remaining_batch in batches:
                    for msg_id in remaining_batch:
                        errors[msg_id] = (None, "Not fetched because an earlier batch exhausted rate-limit retries")
                break

        return _MessageBatchAttempt(results, errors, individual_retry_ids)

    def _get_message_batch[T](
        self, message_ids: list[str], message_format: str, parse_response: Callable[[dict], T]
    ) -> _MessageBatchAttempt[T]:
        results: dict[str, T] = {}
        errors: dict[str, tuple[Exception | None, str]] = {}
        individual_retry_ids: set[str] = set()

        batch_request = self.service.new_batch_http_request()
        for msg_id in message_ids:

            def callback(request_id, response, exception, *, requested_id: str = msg_id) -> None:
                if exception:
                    errors[requested_id] = (exception, str(exception))
                    if not _is_rate_limit_error(exception):
                        individual_retry_ids.add(requested_id)
                    return
                try:
                    results[requested_id] = parse_response(response)
                except Exception as e:
                    errors[requested_id] = (e, str(e))
                    individual_retry_ids.add(requested_id)

            batch_request.add(
                self.service.users().messages().get(userId="me", id=msg_id, format=message_format), callback=callback
            )

        batch_request.execute()

        for msg_id in message_ids:
            if msg_id not in results and msg_id not in errors:
                errors[msg_id] = (None, "Batch response did not contain a result")

        return _MessageBatchAttempt(results, errors, individual_retry_ids)

    def get_label_id(self, label_name: str) -> str | None:
        self._refresh_label_cache_if_needed()
        assert self._label_cache is not None  # _refresh_label_cache_if_needed ensures this
        return self._label_cache.get(label_name)

    def get_or_create_label(self, label_name: str) -> str:
        if label_id := self.get_label_id(label_name):
            return label_id

        # Create label
        label_object = {"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}

        result = self.service.users().labels().create(userId="me", body=label_object).execute()

        # Invalidate cache
        self._label_cache = None

        return str(result["id"])

    def _refresh_label_cache_if_needed(self) -> None:
        if self._label_cache is not None:
            return

        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])

        self._label_cache = {label["name"]: label["id"] for label in labels}

    # Filter operations

    def list_filters(self) -> list[GmailFilter]:
        """List all Gmail filters."""
        result = self.service.users().settings().filters().list(userId="me").execute()
        return [GmailFilter.model_validate(f) for f in result.get("filter", [])]

    def create_filter(self, filter_request: CreateFilterRequest) -> GmailFilter:
        """Create a Gmail filter."""
        result = (
            self.service.users()
            .settings()
            .filters()
            .create(userId="me", body=filter_request.model_dump(by_alias=True, exclude_none=True))
            .execute()
        )
        return GmailFilter.model_validate(result)

    def delete_filter(self, filter_id: str) -> None:
        """Delete a Gmail filter by ID."""
        self.service.users().settings().filters().delete(userId="me", id=filter_id).execute()

    # Label operations

    def list_labels_full(self) -> list[GmailLabel]:
        """List all labels with full metadata."""
        result = self.service.users().labels().list(userId="me").execute()
        return [GmailLabel.model_validate(lbl) for lbl in result.get("labels", [])]

    def delete_label(self, label_id: str) -> None:
        """Delete a label by ID."""
        self.service.users().labels().delete(userId="me", id=label_id).execute()
