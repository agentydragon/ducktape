"""Owner access and read-only routing of the workstation session archive."""

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest_bazel
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

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


def test_session_archive_requires_owner_and_serves_machine_list() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_id, child_id, version_id, payload_id = (uuid4() for _ in range(4))
    empty_session_id, empty_version_id, pending_session_id, failed_session_id = (uuid4() for _ in range(4))
    captured_at = datetime(2026, 1, 1, tzinfo=UTC)
    raw_line = b'{"broken":\xff}\n'
    with make_session_factory(engine)() as db:
        db.add(MachineRow(id="fake-host", display_name="Fake host"))
        db.add(MachineRow(id="idle-host", display_name="Idle host"))
        db.add(MachineRow(id="progress-host", display_name="Progress host"))
        db.add_all(
            [
                SessionRow(
                    id=session_id,
                    machine_id="fake-host",
                    harness="claude",
                    source_path="/fake/root.jsonl",
                    source_relative_path="root.jsonl",
                ),
                SessionRow(
                    id=child_id,
                    machine_id="fake-host",
                    harness="claude",
                    source_path="/fake/child.jsonl",
                    source_relative_path="child.jsonl",
                    parent_session_id=session_id,
                    parent_link_kind="claude_path_inferred",
                    identity_inferred=True,
                ),
                SessionRow(
                    id=empty_session_id,
                    machine_id="fake-host",
                    harness="codex",
                    source_path="/fake/empty.jsonl",
                    source_relative_path="empty.jsonl",
                ),
                SessionRow(
                    id=pending_session_id,
                    machine_id="fake-host",
                    harness="codex",
                    source_path="/fake/pending.jsonl",
                    source_relative_path="pending.jsonl",
                ),
                SessionRow(
                    id=failed_session_id,
                    machine_id="fake-host",
                    harness="codex",
                    source_path="/fake/failed.jsonl",
                    source_relative_path="failed.jsonl",
                ),
                FileVersionRow(
                    id=version_id,
                    session_id=session_id,
                    sha256="a" * 64,
                    byte_size=len(raw_line),
                    source_path="/fake/root.jsonl",
                    first_seen_at=captured_at,
                    last_seen_at=captured_at,
                    first_snapshot_id="b" * 64,
                    complete=False,
                    parse_issue_count=1,
                    record_count=1,
                    is_checkpoint=True,
                    delta_depth=0,
                    parser_version="jsonl-v1",
                ),
                FileVersionRow(
                    id=empty_version_id,
                    session_id=empty_session_id,
                    sha256="d" * 64,
                    byte_size=0,
                    source_path="/fake/empty.jsonl",
                    first_seen_at=captured_at,
                    last_seen_at=captured_at,
                    first_snapshot_id="empty-snapshot",
                    complete=True,
                    parse_issue_count=0,
                    record_count=0,
                    is_checkpoint=True,
                    delta_depth=0,
                    parser_version="jsonl-v1",
                ),
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id="root-snapshot",
                    captured_at=captured_at,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id="empty-snapshot",
                    captured_at=captured_at,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id="pending-snapshot",
                    captured_at=captured_at,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="pending",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id="failed-snapshot",
                    captured_at=captured_at,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="failed",
                    codex_metadata_status="complete",
                ),
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id="root-snapshot",
                    path="/fake/root.jsonl",
                    session_id=session_id,
                    change_kind="added",
                    file_type="file",
                    byte_size=len(raw_line),
                    status="complete",
                    file_version_id=version_id,
                ),
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id="empty-snapshot",
                    path="/fake/empty.jsonl",
                    session_id=empty_session_id,
                    change_kind="added",
                    file_type="file",
                    status="complete",
                    file_version_id=empty_version_id,
                ),
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id="pending-snapshot",
                    path="/fake/pending.jsonl",
                    session_id=pending_session_id,
                    change_kind="added",
                    file_type="file",
                    status="pending",
                ),
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id="failed-snapshot",
                    path="/fake/failed.jsonl",
                    session_id=failed_session_id,
                    change_kind="added",
                    file_type="file",
                    status="failed",
                    error="sanitized test failure",
                ),
                RecordPayloadRow(
                    id=payload_id,
                    sha256="c" * 64,
                    parser_version="jsonl-v1",
                    raw_bytes=raw_line,
                    parse_error="invalid UTF-8",
                ),
                FileVersionRecordRow(file_version_id=version_id, ordinal=0, byte_offset=0, payload_id=payload_id),
            ]
        )
        snapshot_ids = [f"{index}" * 64 for index in range(1, 9)]
        db.add_all(
            [
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[0],
                    captured_at=datetime(2026, 1, 1, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[1],
                    captured_at=datetime(2026, 1, 2, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="pending",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[2],
                    captured_at=datetime(2026, 1, 3, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="failed",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[3],
                    captured_at=datetime(2026, 1, 4, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="partial",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[4],
                    captured_at=datetime(2026, 1, 5, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="failed",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[5],
                    captured_at=datetime(2026, 1, 6, tzinfo=UTC),
                    paths=[],
                    inventory_status="complete",
                    processing_status="pending",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[6],
                    captured_at=datetime(2026, 1, 7, tzinfo=UTC),
                    paths=[],
                    inventory_status="failed",
                    processing_status="failed",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="progress-host",
                    snapshot_id=snapshot_ids[7],
                    captured_at=datetime(2026, 1, 8, tzinfo=UTC),
                    paths=[],
                    inventory_status="pending",
                    processing_status="pending",
                    codex_metadata_status="pending",
                ),
            ]
        )
        db.add_all(
            SnapshotFileRow(
                machine_id="progress-host",
                snapshot_id=snapshot_ids[0],
                path=f"/archive/{index}.jsonl",
                change_kind="added",
                file_type="file",
                byte_size=index,
                status=file_status,
            )
            for index, file_status in enumerate(
                ["complete", "partial", "deleted", "not_indexed", "unclassified", "pending", "pending", "failed"]
            )
        )
        db.commit()
    settings = WebSettings(
        database_url="sqlite://",
        frontend_dir="/fake/frontend",
        public_base_url="http://testserver",
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-client-secret",
        oidc_session_secret="fake-session-secret",
        oidc_allowed_subject="fake-owner",
    )
    client = TestClient(create_app(engine=engine, settings=settings))
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/machines").status_code == 401
    assert client.get("/api/import-progress").status_code == 401

    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "fake-user",
            "expires_at": time.time() + 600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.set("ai-session-archive", TimestampSigner("fake-session-secret").sign(payload).decode())
    machines = client.get("/api/machines")
    assert machines.status_code == 200
    assert machines.headers["cache-control"] == "no-store"
    assert machines.json() == {
        "data": [
            {"id": "fake-host", "display_name": "Fake host"},
            {"id": "idle-host", "display_name": "Idle host"},
            {"id": "progress-host", "display_name": "Progress host"},
        ]
    }
    progress = client.get("/api/import-progress")
    assert progress.headers["cache-control"] == "no-store"
    assert progress.json()["data"] == [
        {
            "machine_id": "fake-host",
            "display_name": "Fake host",
            "known_snapshot_count": 4,
            "completed_snapshot_count": 2,
            "failed_snapshot_count": 1,
            "codex_metadata_pending_snapshot_count": 0,
            "codex_metadata_failed_snapshot_count": 0,
            "codex_metadata_partial_snapshot_count": 0,
            "oldest_unfinished_snapshot_at": "2026-01-01T00:00:00",
            "oldest_unfinished_snapshot_state": "processing_failed",
            "processed_file_count": 2,
            "pending_file_count": 1,
            "failed_file_count": 1,
        },
        {
            "machine_id": "idle-host",
            "display_name": "Idle host",
            "known_snapshot_count": 0,
            "completed_snapshot_count": 0,
            "failed_snapshot_count": 0,
            "codex_metadata_pending_snapshot_count": 0,
            "codex_metadata_failed_snapshot_count": 0,
            "codex_metadata_partial_snapshot_count": 0,
            "oldest_unfinished_snapshot_at": None,
            "oldest_unfinished_snapshot_state": None,
            "processed_file_count": 0,
            "pending_file_count": 0,
            "failed_file_count": 0,
        },
        {
            "machine_id": "progress-host",
            "display_name": "Progress host",
            "known_snapshot_count": 8,
            "completed_snapshot_count": 2,
            "failed_snapshot_count": 3,
            "codex_metadata_pending_snapshot_count": 2,
            "codex_metadata_failed_snapshot_count": 1,
            "codex_metadata_partial_snapshot_count": 1,
            "oldest_unfinished_snapshot_at": "2026-01-02T00:00:00",
            "oldest_unfinished_snapshot_state": "codex_metadata_pending",
            "processed_file_count": 5,
            "pending_file_count": 2,
            "failed_file_count": 1,
        },
    ]
    assert client.get("/api/sessions", params={"cursor": "invalid!"}).status_code == 400
    assert client.get(f"/api/sessions/{session_id}").json()["id"] == str(session_id)
    children = client.get("/api/sessions", params={"parent_session_id": str(session_id)}).json()
    assert [child["id"] for child in children["data"]] == [str(child_id)]
    session_rows = client.get("/api/sessions", params={"machine_id": "fake-host", "limit": 500}).json()["data"]
    sessions_by_id = {row["id"]: row for row in session_rows}
    assert sessions_by_id[str(session_id)]["transcript_status"] == "captured"
    assert sessions_by_id[str(session_id)]["latest_version_record_count"] == 1
    assert sessions_by_id[str(child_id)]["transcript_status"] == "unavailable"
    assert sessions_by_id[str(empty_session_id)]["transcript_status"] == "captured"
    assert sessions_by_id[str(empty_session_id)]["latest_version_record_count"] == 0
    assert sessions_by_id[str(pending_session_id)]["transcript_status"] == "pending"
    assert sessions_by_id[str(failed_session_id)]["transcript_status"] == "failed"
    versions = client.get(f"/api/sessions/{session_id}/versions").json()["data"]
    assert [version["id"] for version in versions] == [str(version_id)]
    events = client.get(f"/api/sessions/{session_id}/versions/{version_id}/events").json()["data"]
    assert base64.b64decode(events[0]["raw_base64"]) == raw_line
    assert events[0]["parse_error"] is not None
    assert client.get(f"/api/sessions/{child_id}/versions/{version_id}/events").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}/versions").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}/versions/{uuid4()}/events").status_code == 404


def test_event_backward_pagination_reads_delta_version_lineage() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_id, checkpoint_id, delta_id = (uuid4() for _ in range(3))
    checkpoint_snapshot_id = "a" * 64
    delta_snapshot_id = "b" * 64
    captured_at = datetime(2026, 1, 1, tzinfo=UTC)
    with make_session_factory(engine)() as db:
        db.add(MachineRow(id="fake-host", display_name="Fake host"))
        db.flush()
        db.add(
            SessionRow(
                id=session_id,
                machine_id="fake-host",
                harness="claude",
                source_path="/fake/pagination.jsonl",
                source_relative_path="pagination.jsonl",
            )
        )
        db.add_all(
            [
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id=checkpoint_snapshot_id,
                    captured_at=captured_at,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                ),
                ResticSnapshotRow(
                    machine_id="fake-host",
                    snapshot_id=delta_snapshot_id,
                    captured_at=captured_at,
                    parent_snapshot_id=checkpoint_snapshot_id,
                    paths=["/fake"],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                ),
            ]
        )
        db.flush()
        db.add(
            FileVersionRow(
                id=checkpoint_id,
                session_id=session_id,
                sha256="c" * 64,
                byte_size=80,
                source_path="/fake/pagination.jsonl",
                first_seen_at=captured_at,
                last_seen_at=captured_at,
                first_snapshot_id=checkpoint_snapshot_id,
                complete=True,
                parse_issue_count=0,
                record_count=4,
                is_checkpoint=True,
                delta_depth=0,
                parser_version="jsonl-v1",
            )
        )
        db.flush()
        db.add(
            FileVersionRow(
                id=delta_id,
                session_id=session_id,
                sha256="d" * 64,
                byte_size=140,
                source_path="/fake/pagination.jsonl",
                first_seen_at=captured_at,
                last_seen_at=captured_at,
                first_snapshot_id=delta_snapshot_id,
                complete=True,
                parse_issue_count=0,
                record_count=7,
                parent_version_id=checkpoint_id,
                shared_prefix_record_count=4,
                is_checkpoint=False,
                delta_depth=1,
                parser_version="jsonl-v1",
            )
        )
        db.flush()
        payloads = []
        records = []
        for ordinal in range(7):
            payload_id = uuid4()
            raw_bytes = f'{{"ordinal":{ordinal}}}\n'.encode()
            payloads.append(
                RecordPayloadRow(
                    id=payload_id,
                    sha256=f"{ordinal + 1:064x}",
                    parser_version="jsonl-v1",
                    raw_bytes=raw_bytes,
                    kind="test",
                )
            )
            records.append(
                FileVersionRecordRow(
                    file_version_id=checkpoint_id if ordinal < 4 else delta_id,
                    ordinal=ordinal,
                    byte_offset=ordinal * 16,
                    payload_id=payload_id,
                )
            )
        db.add_all(payloads)
        db.add_all(records)
        db.add_all(
            [
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id=checkpoint_snapshot_id,
                    path="/fake/pagination.jsonl",
                    session_id=session_id,
                    change_kind="added",
                    file_type="file",
                    byte_size=80,
                    status="complete",
                    file_version_id=checkpoint_id,
                ),
                SnapshotFileRow(
                    machine_id="fake-host",
                    snapshot_id=delta_snapshot_id,
                    path="/fake/pagination.jsonl",
                    session_id=session_id,
                    change_kind="modified",
                    file_type="file",
                    byte_size=140,
                    status="complete",
                    file_version_id=delta_id,
                ),
            ]
        )
        db.commit()

    settings = WebSettings(
        database_url="sqlite://",
        frontend_dir="/fake/frontend",
        public_base_url="http://testserver",
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-client-secret",
        oidc_session_secret="fake-session-secret",
        oidc_allowed_subject="fake-owner",
    )
    client = TestClient(create_app(engine=engine, settings=settings))
    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "fake-user",
            "expires_at": time.time() + 600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.set("ai-session-archive", TimestampSigner("fake-session-secret").sign(payload).decode())
    events_url = f"/api/sessions/{session_id}/versions/{delta_id}/events"

    first_forward = client.get(events_url, params={"limit": 2}).json()
    assert [event["ordinal"] for event in first_forward["data"]] == [0, 1]
    assert first_forward["next_cursor"] == 1
    second_forward = client.get(events_url, params={"cursor": 1, "limit": 2}).json()
    assert [event["ordinal"] for event in second_forward["data"]] == [2, 3]
    assert second_forward["next_cursor"] == 3

    first_backward = client.get(events_url, params={"direction": "backward", "limit": 2}).json()
    assert [event["ordinal"] for event in first_backward["data"]] == [5, 6]
    assert first_backward["next_cursor"] == 5
    middle_backward = client.get(events_url, params={"direction": "backward", "cursor": 5, "limit": 2}).json()
    assert [event["ordinal"] for event in middle_backward["data"]] == [3, 4]
    assert [event["parsed"]["ordinal"] for event in middle_backward["data"]] == [3, 4]
    assert middle_backward["next_cursor"] == 3
    final_backward = client.get(events_url, params={"direction": "backward", "cursor": 3, "limit": 2}).json()
    assert [event["ordinal"] for event in final_backward["data"]] == [1, 2]
    assert final_backward["next_cursor"] == 1
    exhausted_backward = client.get(events_url, params={"direction": "backward", "cursor": 1, "limit": 2}).json()
    assert [event["ordinal"] for event in exhausted_backward["data"]] == [0]
    assert exhausted_backward["next_cursor"] is None
    empty_backward = client.get(events_url, params={"direction": "backward", "cursor": 0, "limit": 2}).json()
    assert empty_backward == {"data": [], "next_cursor": None}


def test_login_routes_are_reachable_with_the_frontend_mounted(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>archive frontend</main>")
    (assets / "app.js").write_text("console.log('archive frontend')")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    settings = WebSettings(
        database_url="sqlite://",
        frontend_dir=frontend,
        public_base_url="http://testserver",
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-client-secret",
        oidc_session_secret="fake-session-secret",
        oidc_allowed_subject="fake-owner",
    )
    app = create_app(engine=engine, settings=settings, frontend_dir=frontend)

    class FakeOAuthClient:
        async def authorize_redirect(self, request: object, redirect_uri: str) -> RedirectResponse:
            assert redirect_uri == "http://testserver/auth/callback"
            return RedirectResponse("https://idp.example.invalid/authorize")

        async def authorize_access_token(self, request: object) -> dict[str, object]:
            return {}

    class FakeOAuth:
        def create_client(self, name: str) -> FakeOAuthClient:
            assert name == "authentik"
            return FakeOAuthClient()

    # Keep the login route real while replacing only the external OIDC client boundary.
    app.state.oauth = FakeOAuth()
    client = TestClient(app)

    session = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "fake-user",
            "expires_at": time.time() + 600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.set("ai-session-archive", TimestampSigner("fake-session-secret").sign(payload).decode())

    home = client.get("/", follow_redirects=False)
    deep_link = client.get("/sessions/fake-session-id?machine=fake-host", follow_redirects=False)
    asset = client.get("/assets/app.js", follow_redirects=False)
    missing_asset = client.get("/assets/missing.js", follow_redirects=False)
    missing_page = client.get("/unknown-page", follow_redirects=False)
    missing_api = client.get("/api/unknown", follow_redirects=False)
    login = client.get("/auth/login", follow_redirects=False)
    callback = client.get("/auth/callback", params={"error": "access_denied", "state": "fake"})
    logout = client.post("/auth/logout", headers={"Origin": settings.public_base_url}, follow_redirects=False)
    signed_out = client.get("/auth/signed-out", follow_redirects=False)

    assert (home.status_code, home.text) == (200, "<main>archive frontend</main>")
    assert (deep_link.status_code, deep_link.text) == (200, home.text)
    assert deep_link.headers["cache-control"] == "no-store"
    assert (asset.status_code, asset.text) == (200, "console.log('archive frontend')")
    assert (missing_asset.status_code, missing_page.status_code, missing_api.status_code) == (404, 404, 404)
    assert "archive frontend" not in missing_api.text
    assert (login.status_code, login.headers["location"]) == (307, "https://idp.example.invalid/authorize")
    assert (callback.status_code, "Authentik sign-in failed." in callback.text) == (401, True)
    assert (logout.status_code, logout.headers["location"]) == (303, "/auth/signed-out")
    assert (signed_out.status_code, "You are signed out." in signed_out.text) == (200, True)

    unauthenticated = TestClient(app).get("/sessions/fake-session-id", follow_redirects=False)
    assert unauthenticated.status_code in {302, 303, 307}
    assert unauthenticated.headers["location"].startswith("/auth/login")


if __name__ == "__main__":
    pytest_bazel.main()
