"""Gmail API client wrapper."""

import base64
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .gmail_api_models import (
    CreateFilterRequest,
    GmailFilter,
    GmailLabel,
    GmailMessageMinimal,
    GmailMessageWithHeaders,
    SystemLabel,
    resolve_label_id,
)
from .models import Email

logger = logging.getLogger(__name__)


def _is_rate_limit_error(exception):
    if isinstance(exception, HttpError):
        return exception.resp.status in (429, 503)
    return False


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
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
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
        except (ValueError, TypeError):
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


class GmailClient:
    """Wrapper around Gmail API for email archival operations.

    TODO: Add a `modify_labels_batch` method that uses batchModify for bulk label
    operations (add/remove multiple labels at once). The existing `add_labels_batch`
    only handles adding a single label. Ad-hoc scripts currently loop through messages
    individually which is slow - batchModify can process up to 1000 messages per call.
    """

    def __init__(self, token_file: Path):
        self.token_file = token_file
        self.service = self._build_service()
        self._label_cache: dict[str, str] | None = None  # name -> id

    def _build_service(self):
        creds = Credentials.from_authorized_user_file(self.token_file)
        return build("gmail", "v1", credentials=creds)

    def list_messages_by_labels(self, label_names: list[str], max_results: int | None = None) -> list[str]:
        """List message IDs with all given labels (AND operation)."""
        # Resolve all label names to IDs
        self._refresh_label_cache_if_needed()
        assert self._label_cache is not None  # _refresh_label_cache_if_needed ensures this
        label_ids = []
        for name in label_names:
            try:
                label_ids.append(resolve_label_id(name, self._label_cache))
            except ValueError:
                return []  # Label doesn't exist

        return self._list_messages({"labelIds": label_ids}, max_results)

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
    ) -> list[GmailMessageWithHeaders]:
        """Fetch message metadata (headers only) using batch requests.

        Uses format=metadata to get headers without body content.
        Returns GmailMessageWithHeaders objects with full payload.headers.
        """
        results: list[GmailMessageWithHeaders] = []
        all_errors = []

        def create_callback(results_list, errors_list):
            def callback(request_id, response, exception):
                if exception:
                    errors_list.append((request_id, str(exception)))
                    return
                try:
                    results_list.append(GmailMessageWithHeaders.model_validate(response))
                except Exception as e:
                    errors_list.append((request_id, str(e)))

            return callback

        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]
            batch_results: list[GmailMessageWithHeaders] = []
            batch_errors: list[tuple[str, str]] = []

            batch_request = self.service.new_batch_http_request()
            for msg_id in batch:
                batch_request.add(
                    self.service.users().messages().get(userId="me", id=msg_id, format="metadata"),
                    callback=create_callback(batch_results, batch_errors),
                )

            batch_request.execute()
            results.extend(batch_results)
            all_errors.extend(batch_errors)

        if all_errors:
            print(f"Warning: Failed to fetch metadata for {len(all_errors)} messages", file=sys.stderr)

        return results

    def get_messages_minimal_batch(self, message_ids: list[str], batch_size: int = 100) -> list[GmailMessageMinimal]:
        """Fetch minimal message metadata (id, labels, snippet, date) using batch requests.

        This is more efficient than fetching full messages when you only need label info.
        Returns GmailMessageMinimal (no raw bytes - for that use get_messages_batch).
        """
        results = []
        all_errors = []

        def create_callback(results_list, errors_list):
            def callback(request_id, response, exception):
                if exception:
                    errors_list.append((request_id, str(exception)))
                    return
                try:
                    results_list.append(GmailMessageMinimal.model_validate(response))
                except Exception as e:
                    errors_list.append((request_id, str(e)))

            return callback

        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]
            batch_results: list[GmailMessageMinimal] = []
            batch_errors: list[tuple[str, str]] = []

            batch_request = self.service.new_batch_http_request()
            for msg_id in batch:
                batch_request.add(
                    self.service.users().messages().get(userId="me", id=msg_id, format="minimal"),
                    callback=create_callback(batch_results, batch_errors),
                )

            batch_request.execute()
            results.extend(batch_results)
            all_errors.extend(batch_errors)

        if all_errors:
            print(f"Warning: Failed to fetch {len(all_errors)} messages", file=sys.stderr)

        return results

    def get_messages_raw_batch(
        self, message_ids: list[str], batch_size: int, retry_failures: bool = True
    ) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
        """Fetch raw message bytes using batch requests.

        Returns (successful_results, failed_results) where each is a list of (message_id, data) tuples.
        """
        results = []
        all_errors = []
        failed_message_ids = []

        def create_callback(msg_id, results_list, errors_list, failed_ids_list):
            """Create callback that captures the actual message ID and exception."""

            def callback(request_id, response, exception):
                if exception:
                    error_msg = str(exception)
                    # Store (msg_id, exception, error_msg) for proper error checking
                    errors_list.append((msg_id, exception, error_msg))
                    # Only retry individually if it's NOT a rate limit error
                    # (batch retry with exponential backoff already handled 429/503)
                    if not _is_rate_limit_error(exception):
                        failed_ids_list.append(msg_id)
                    return
                try:
                    raw_bytes = base64.urlsafe_b64decode(response["raw"])
                    results_list.append((response["id"], raw_bytes))
                except Exception as e:
                    error_msg = str(e)
                    # Decoding errors don't have an HttpError exception
                    errors_list.append((response["id"], None, error_msg))
                    # Decoding errors are worth retrying individually
                    failed_ids_list.append(response["id"])

            return callback

        # Process in batches (API allows 100, but 50 recommended to avoid rate limits)
        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]

            # Retry logic for the entire batch if rate limited
            max_retries = 10
            batch_succeeded = False
            for attempt in range(1, max_retries + 1):
                batch_results: list[tuple[str, bytes]] = []
                batch_errors: list[tuple[str, Exception | None, str]] = []
                batch_failed_ids: list[str] = []

                # Use service.new_batch_http_request() to get correct batch endpoint
                batch_request = self.service.new_batch_http_request()
                for msg_id in batch:
                    batch_request.add(
                        self.service.users().messages().get(userId="me", id=msg_id, format="raw"),
                        callback=create_callback(msg_id, batch_results, batch_errors, batch_failed_ids),
                    )

                # Execute batch
                batch_request.execute()

                # Check if any requests were rate limited
                rate_limit_errors: list[HttpError] = [
                    exc
                    for _, exc, _ in batch_errors
                    if exc is not None and _is_rate_limit_error(exc) and isinstance(exc, HttpError)
                ]
                rate_limited = len(rate_limit_errors) > 0

                if not rate_limited:
                    # Success! Add results and move to next batch
                    results.extend(batch_results)
                    all_errors.extend(batch_errors)
                    failed_message_ids.extend(batch_failed_ids)
                    batch_succeeded = True
                    break

                # Rate limited - wait and retry
                if attempt < max_retries:
                    # Check for Retry-After timing in any of the rate limit errors
                    retry_results = [_get_retry_after(exc) for exc in rate_limit_errors]
                    retry_after_values = [v for v, _ in retry_results if v is not None]

                    # Print debug info from first rate limit error
                    if retry_results and attempt == 1:  # Only print on first attempt to avoid spam
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

                    if retry_after_values:
                        # Use the maximum Retry-After value if multiple are present
                        wait_time = max(retry_after_values)
                        print(
                            f"⚠️  Rate limited (attempt {attempt}), server says retry after {wait_time}s...",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        # No Retry-After info, use exponential backoff
                        wait_time = min(2**attempt, 60)
                        print(
                            f"⚠️  Rate limited (attempt {attempt}), retrying batch in {wait_time:.1f}s...",
                            file=sys.stderr,
                            flush=True,
                        )
                    time.sleep(wait_time)
                else:
                    # Max retries exceeded - abort entire operation
                    print(f"✗ Batch failed after {max_retries} retries - aborting", file=sys.stderr, flush=True)
                    results.extend(batch_results)
                    all_errors.extend(batch_errors)
                    failed_message_ids.extend(batch_failed_ids)
                    break

            # If batch failed after all retries, abort the entire operation
            if not batch_succeeded:
                print(
                    f"Aborting download - processed {len(results)} messages before failure", file=sys.stderr, flush=True
                )
                break

        # Retry failed messages individually (only for non-rate-limit errors)
        # Rate limit errors (429/503) are already handled by batch retry with exponential backoff
        if retry_failures and failed_message_ids:
            print(
                f"Retrying {len(failed_message_ids)} failed messages individually (non-rate-limit errors)...",
                file=sys.stderr,
            )

            for msg_id in failed_message_ids:
                try:
                    msg = self.service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
                    raw_bytes = base64.urlsafe_b64decode(msg["raw"])
                    results.append((msg_id, raw_bytes))
                    # Remove from errors list since retry succeeded
                    all_errors = [(err_id, exc, err_msg) for err_id, exc, err_msg in all_errors if err_id != msg_id]
                except Exception as e:
                    # Update error with retry failure info
                    error_msg = f"Retry failed: {e!s}"
                    print(f"  Failed retry for {msg_id}: {error_msg[:100]}", file=sys.stderr)
                    all_errors = [(err_id, exc, err_msg) for err_id, exc, err_msg in all_errors if err_id != msg_id]
                    all_errors.append((msg_id, None, error_msg))

        # Convert 3-tuples (msg_id, exception, error_msg) to 2-tuples (msg_id, error_msg) for callers
        return results, [(msg_id, err_msg) for msg_id, _, err_msg in all_errors]

    def get_messages_batch(self, message_ids: list[str], batch_size: int) -> list[Email]:
        emails = []

        def create_callback(results_list, errors_list):
            def callback(request_id, response, exception):
                if exception:
                    errors_list.append((request_id, str(exception)))
                    return
                try:
                    raw_bytes = base64.urlsafe_b64decode(response["raw"])
                    metadata = GmailMessageMinimal.model_validate(response)
                    results_list.append(Email.from_gmail_response(raw_bytes, metadata))
                except Exception as e:
                    errors_list.append((request_id, str(e)))

            return callback

        # Process in batches
        all_errors = []
        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]
            batch_emails: list[Email] = []
            batch_errors: list[tuple[str, str]] = []

            batch_request = self.service.new_batch_http_request()
            for msg_id in batch:
                batch_request.add(
                    self.service.users().messages().get(userId="me", id=msg_id, format="raw"),
                    callback=create_callback(batch_emails, batch_errors),
                )

            batch_request.execute()
            emails.extend(batch_emails)
            all_errors.extend(batch_errors)

        if all_errors:
            # Log errors but don't fail completely
            print(f"Warning: Failed to fetch {len(all_errors)} messages:", file=sys.stderr)
            for req_id, error in all_errors[:5]:  # Show first 5
                print(f"  - Request {req_id}: {error}", file=sys.stderr)
            if len(all_errors) > 5:
                print(f"  ... and {len(all_errors) - 5} more errors", file=sys.stderr)

        return emails

    def get_message(self, message_id: str) -> Email:
        response = self.service.users().messages().get(userId="me", id=message_id, format="raw").execute()
        raw_bytes = base64.urlsafe_b64decode(response["raw"])
        metadata = GmailMessageMinimal.model_validate(response)
        return Email.from_gmail_response(raw_bytes, metadata)

    def add_label(self, message_id: str, label_name: str) -> None:
        label_id = self.get_or_create_label(label_name)

        self.service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()

    def add_labels_batch(
        self,
        message_ids: list[str],
        label_name: str,
        batch_size: int = 1000,
        progress_callback: Callable[[int], None] | None = None,
        archive: bool = False,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Add label to multiple messages. Returns (successful_ids, failed_ids_with_errors)."""
        label_id = self.get_or_create_label(label_name)

        successful = []
        failed = []

        # Process in batches
        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]

            try:
                body = {"ids": batch, "addLabelIds": [label_id]}
                if archive:
                    body["removeLabelIds"] = [SystemLabel.INBOX]

                self.service.users().messages().batchModify(userId="me", body=body).execute()
                successful.extend(batch)
                if progress_callback:
                    progress_callback(len(batch))
            except Exception:
                # Batch failed, retry individually to identify failures
                for msg_id in batch:
                    try:
                        body = {"addLabelIds": [label_id]}
                        if archive:
                            body["removeLabelIds"] = [SystemLabel.INBOX]
                        self.service.users().messages().modify(userId="me", id=msg_id, body=body).execute()
                        successful.append(msg_id)
                        if progress_callback:
                            progress_callback(1)
                    except Exception as individual_error:
                        failed.append((msg_id, str(individual_error)))
                        if progress_callback:
                            progress_callback(1)

        return successful, failed

    def remove_label(self, message_id: str, label_name: str) -> None:
        if not (label_id := self.get_label_id(label_name)):
            return

        self.service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": [label_id]}
        ).execute()

    def remove_from_inbox(self, message_id: str) -> None:
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": [SystemLabel.INBOX]}
        ).execute()

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
