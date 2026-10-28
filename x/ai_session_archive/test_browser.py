"""Browser-level read of real, owner-authenticated archived transcript data."""

# gazelle:include_dep //util/testing:visual_fixtures

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from itsdangerous import TimestampSigner
from playwright.async_api import Playwright, expect
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DbSession

from util.testing.asgi import serve_app
from util.testing.frontend_visual import deterministic_browser_context, disable_animations_script
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage
from x.ai_session_archive.database import (
    Base,
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
    make_session_factory,
)
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

_PARSER_VERSION = "jsonl-v1"


async def test_authenticated_browser_reads_archived_transcript(playwright_driver: Playwright, tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'archive.sqlite'}")
    Base.metadata.create_all(engine)
    session_id, child_id, older_id, latest_id = _seed_archive(engine)
    settings = WebSettings(
        database_url="sqlite://",
        public_base_url="http://127.0.0.1",
        oidc_issuer="https://identity.example.invalid",
        oidc_client_id="archive-test",
        oidc_client_secret="archive-test-client-secret",
        oidc_session_secret="archive-test-session-secret",
        oidc_allowed_subject="archive-test-owner",
    )
    app = create_app(engine=engine, settings=settings)

    async with serve_app(app) as origin:
        async with httpx.AsyncClient() as unauthenticated:
            response = await unauthenticated.get(f"{origin}/api/machines")
        assert response.status_code == 401

        session_cookie = _owner_session_cookie(settings)
        async with deterministic_browser_context(
            playwright_driver, viewport={"width": 1440, "height": 1000}, frozen_now_ms=1_767_264_000_000
        ) as context:
            await context.add_init_script(disable_animations_script())
            await context.add_cookies(
                [
                    {
                        "name": "ai-session-archive",
                        "value": session_cookie,
                        "url": origin,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = await context.new_page()
            browser_errors: list[str] = []
            page.on("pageerror", lambda error: browser_errors.append(str(error)))
            page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
            api_responses: list[tuple[str, int]] = []
            page.on(
                "response",
                lambda response: (
                    api_responses.append((response.url, response.status)) if "/api/" in response.url else None
                ),
            )
            visual = VisualPage(
                page,
                output_dir=undeclared_outputs_dir(),
                title="Authenticated workstation session archive",
                capture_name="authenticated_archived_transcript",
            )
            await page.goto(origin, wait_until="domcontentloaded")
            try:
                await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
            except AssertionError:
                output_dir = undeclared_outputs_dir()
                (output_dir / "browser-startup-errors.json").write_text(
                    json.dumps(
                        {"url": page.url, "body": await page.locator("body").inner_text(), "errors": browser_errors},
                        indent=2,
                    )
                )
                await page.screenshot(path=output_dir / "browser-startup.png")
                raise
            await expect(page.get_by_text("Atlas", exact=True).first).to_be_visible()

            transcript = page.locator('[aria-label="Session transcript"]')
            await expect(page.get_by_role("link", name="Review archived conversation")).to_be_visible()
            await expect(transcript.get_by_role("heading", name="Review archived conversation")).to_be_visible()
            await expect(transcript).to_contain_text("Latest transcript from the archived version.")

            version_picker = page.get_by_label("Captured version")
            await page.goto(f"{origin}/sessions/{session_id}?version={older_id}", wait_until="domcontentloaded")
            await expect(transcript).to_contain_text("Earlier transcript capture.")
            await expect(version_picker).to_have_value(str(older_id))
            await version_picker.select_option(str(latest_id))
            await expect(transcript).to_contain_text("Latest transcript from the archived version.")
            await page.go_back()
            await expect(transcript).to_contain_text("Earlier transcript capture.")
            await page.go_forward()
            await expect(transcript).to_contain_text("Latest transcript from the archived version.")
            await page.reload(wait_until="domcontentloaded")
            await expect(transcript).to_contain_text("Latest transcript from the archived version.")
            await expect(transcript).to_contain_text("This JSONL line could not be parsed")
            malformed_record = transcript.locator('[data-event-ordinal="3"]')
            await malformed_record.locator("details").first.locator("summary").first.click()
            await expect(malformed_record).to_contain_text('{"type":"assistant","message":')

            related = page.get_by_role("region", name="Related sessions")
            await expect(related).to_contain_text("Review subagent notes")
            await related.get_by_role("link", name="Review subagent notes").click()
            await expect(transcript.get_by_role("heading", name="Review subagent notes")).to_be_visible()
            await expect(transcript).to_contain_text("Child answer loaded from archived records.")
            await expect(related).to_contain_text("Parent inferred from Claude subagent path")
            await expect(related).to_contain_text("Review archived conversation")

            session_list = page.locator('[aria-label="Session list"]')
            await session_list.get_by_role("link", name="Inspect conflicting Codex metadata").click()
            await expect(transcript).to_contain_text("Conflicting native session IDs")
            await expect(transcript).to_contain_text("Native session ID candidates: thread-child-a, thread-child-b")
            await expect(transcript).to_contain_text("Conflicting Codex parent_thread_id values")
            await expect(transcript).to_contain_text(
                "Conflicting native parent ID candidates: thread-parent-a, thread-parent-b"
            )
            await expect(transcript).to_contain_text("Parent session could not be resolved to a captured session.")

            await session_list.get_by_role("link", name="Review archived conversation", exact=False).click()
            await expect(transcript).to_contain_text("Latest transcript from the archived version.")
            malformed_record = transcript.locator('[data-event-ordinal="3"]')
            await malformed_record.locator("details").first.locator("summary").first.click()
            await expect(malformed_record).to_contain_text('{"type":"assistant","message":')

            assert api_responses
            failed_responses = [(url, status) for url, status in api_responses if status != 200]
            assert not failed_responses, f"API requests failed: {failed_responses!r}"
            assert any(f"/api/sessions/{session_id}/versions" in url for url, _ in api_responses)
            assert any(f"/api/sessions/{child_id}/versions" in url for url, _ in api_responses)
            await page.mouse.move(0, 0)
            await visual.capture(target=page.locator("#root"))

    engine.dispose()


def _seed_archive(engine: Engine) -> tuple[UUID, UUID, UUID, UUID]:
    session_id, child_id, older_id, latest_id, child_version_id = (uuid4() for _ in range(5))
    earlier_snapshot, child_snapshot, latest_snapshot = (f"{digit}" * 64 for digit in ("1", "2", "3"))
    start = datetime(2026, 1, 2, 9, tzinfo=UTC)
    older_time = start + timedelta(minutes=1)
    child_time = start + timedelta(minutes=2)
    latest_time = start + timedelta(minutes=3)
    root_path = "/archive/claude/projects/demo/root.jsonl"
    child_path = "/archive/claude/projects/demo/subagents/agent-1.jsonl"
    codex_conflict_path = "/archive/codex/sessions/2026/10/28/00000000.jsonl"

    old_records = [
        _jsonl_record(
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "Review the archive."}]},
                "timestamp": older_time.isoformat(),
            },
            older_time,
        ),
        _jsonl_record(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Earlier transcript capture."}]},
                "timestamp": (older_time + timedelta(seconds=1)).isoformat(),
            },
            older_time + timedelta(seconds=1),
        ),
    ]
    latest_suffix = [
        _jsonl_record(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Latest transcript from the archived version."}]},
                "timestamp": latest_time.isoformat(),
            },
            latest_time,
        ),
        (
            b'{"type":"assistant","message":\n',
            None,
            latest_time + timedelta(seconds=1),
            "assistant",
            "Unexpected end of JSON input",
        ),
    ]
    child_records = [
        _jsonl_record(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Child answer loaded from archived records."}]},
                "timestamp": child_time.isoformat(),
            },
            child_time,
        )
    ]

    with make_session_factory(engine)() as db:
        db.add(MachineRow(id="atlas", display_name="Atlas"))
        db.add_all(
            [
                SessionRow(
                    id=session_id,
                    machine_id="atlas",
                    harness="claude",
                    source_path=root_path,
                    source_relative_path="projects/demo/root.jsonl",
                    title="Review archived conversation",
                    cwd="/home/example/project",
                    started_at=start,
                    last_activity_at=latest_time,
                ),
                SessionRow(
                    id=child_id,
                    machine_id="atlas",
                    harness="claude",
                    source_path=child_path,
                    source_relative_path="projects/demo/subagents/agent-1.jsonl",
                    parent_session_id=session_id,
                    parent_link_kind="claude_path_inferred",
                    title="Review subagent notes",
                    cwd="/home/example/project",
                    started_at=child_time,
                    last_activity_at=child_time,
                ),
                SessionRow(
                    id=uuid4(),
                    machine_id="atlas",
                    harness="codex",
                    source_path=codex_conflict_path,
                    source_relative_path="sessions/2026/10/28/00000000.jsonl",
                    native_session_id_conflict=True,
                    native_session_id_candidates=["thread-child-a", "thread-child-b"],
                    parent_native_session_id_conflict=True,
                    parent_native_session_id_candidates=["thread-parent-a", "thread-parent-b"],
                    parent_link_kind="codex_parent_thread_id_inconsistent",
                    title="Inspect conflicting Codex metadata",
                    cwd="/home/example/project",
                    started_at=child_time,
                    last_activity_at=child_time + timedelta(seconds=30),
                ),
            ]
        )

        _add_archive_version(
            db,
            session_id=session_id,
            version_id=older_id,
            snapshot_id=earlier_snapshot,
            source_path=root_path,
            captured_at=older_time,
            records=old_records,
            complete=True,
            parent_version_id=None,
            shared_prefix_record_count=0,
            delta_depth=0,
            is_checkpoint=True,
        )
        _add_archive_version(
            db,
            session_id=session_id,
            version_id=latest_id,
            snapshot_id=latest_snapshot,
            source_path=root_path,
            captured_at=latest_time,
            records=[*old_records, *latest_suffix],
            stored_records=latest_suffix,
            complete=False,
            parse_issue_count=1,
            parent_version_id=older_id,
            shared_prefix_record_count=len(old_records),
            delta_depth=1,
            is_checkpoint=False,
        )
        _add_archive_version(
            db,
            session_id=child_id,
            version_id=child_version_id,
            snapshot_id=child_snapshot,
            source_path=child_path,
            captured_at=child_time,
            records=child_records,
            complete=True,
            parent_version_id=None,
            shared_prefix_record_count=0,
            delta_depth=0,
            is_checkpoint=True,
        )
        db.add_all(
            [
                ResticSnapshotRow(
                    machine_id="atlas",
                    snapshot_id=earlier_snapshot,
                    captured_at=older_time,
                    paths=["/archive/claude/projects/demo/root.jsonl"],
                    inventory_status="complete",
                    processing_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="atlas",
                    snapshot_id=child_snapshot,
                    captured_at=child_time,
                    paths=["/archive/claude/projects/demo/subagents/agent-1.jsonl"],
                    inventory_status="complete",
                    processing_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="atlas",
                    snapshot_id=latest_snapshot,
                    captured_at=latest_time,
                    paths=["/archive/claude/projects/demo/root.jsonl"],
                    inventory_status="complete",
                    processing_status="complete",
                ),
                SnapshotFileRow(
                    machine_id="atlas",
                    snapshot_id=earlier_snapshot,
                    path=root_path,
                    session_id=session_id,
                    change_kind="added",
                    file_type="file",
                    byte_size=sum(len(record[0]) for record in old_records),
                    status="complete",
                    file_version_id=older_id,
                ),
                SnapshotFileRow(
                    machine_id="atlas",
                    snapshot_id=child_snapshot,
                    path=child_path,
                    session_id=child_id,
                    change_kind="added",
                    file_type="file",
                    byte_size=sum(len(record[0]) for record in child_records),
                    status="complete",
                    file_version_id=child_version_id,
                ),
                SnapshotFileRow(
                    machine_id="atlas",
                    snapshot_id=latest_snapshot,
                    path=root_path,
                    session_id=session_id,
                    change_kind="changed",
                    file_type="file",
                    byte_size=sum(len(record[0]) for record in old_records + latest_suffix),
                    status="partial",
                    file_version_id=latest_id,
                ),
            ]
        )
        db.commit()

    return session_id, child_id, older_id, latest_id


def _add_archive_version(
    db: DbSession,
    *,
    session_id: UUID,
    version_id: UUID,
    snapshot_id: str,
    source_path: str,
    captured_at: datetime,
    records: Sequence[tuple[bytes, object | None, datetime | None, str | None, str | None]],
    complete: bool,
    parent_version_id: UUID | None,
    shared_prefix_record_count: int,
    delta_depth: int,
    is_checkpoint: bool,
    stored_records: Sequence[tuple[bytes, object | None, datetime | None, str | None, str | None]] | None = None,
    parse_issue_count: int = 0,
) -> None:
    content = b"".join(record[0] for record in records)
    db.add(
        FileVersionRow(
            id=version_id,
            session_id=session_id,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            source_path=source_path,
            first_seen_at=captured_at,
            last_seen_at=captured_at,
            first_snapshot_id=snapshot_id,
            complete=complete,
            parse_issue_count=parse_issue_count,
            record_count=len(records),
            parent_version_id=parent_version_id,
            shared_prefix_record_count=shared_prefix_record_count,
            is_checkpoint=is_checkpoint,
            delta_depth=delta_depth,
            parser_version=_PARSER_VERSION,
        )
    )
    offset = sum(len(record[0]) for record in records[:shared_prefix_record_count])
    stored = records[shared_prefix_record_count:] if stored_records is None else stored_records
    for ordinal, (raw, parsed, timestamp, kind, parse_error) in enumerate(stored, start=shared_prefix_record_count):
        payload_id = uuid4()
        db.add(
            RecordPayloadRow(
                id=payload_id,
                sha256=hashlib.sha256(raw).hexdigest(),
                parser_version=_PARSER_VERSION,
                raw_bytes=raw,
                parsed=parsed,
                parse_error=parse_error,
                timestamp=timestamp,
                kind=kind,
            )
        )
        db.add(
            FileVersionRecordRow(file_version_id=version_id, ordinal=ordinal, byte_offset=offset, payload_id=payload_id)
        )
        offset += len(raw)


def _jsonl_record(parsed: dict[str, object], timestamp: datetime) -> tuple[bytes, object, datetime, str, None]:
    raw = json.dumps(parsed, separators=(",", ":")).encode("utf-8") + b"\n"
    return raw, parsed, timestamp, str(parsed["type"]), None


def _owner_session_cookie(settings: WebSettings) -> str:
    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "archive-test-owner",
            "expires_at": time.time() + 600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode("utf-8"))
    return TimestampSigner(settings.oidc_session_secret.get_secret_value()).sign(payload).decode("utf-8")


if __name__ == "__main__":
    pytest_bazel.main()
