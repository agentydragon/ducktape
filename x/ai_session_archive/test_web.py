"""Owner access and read-only routing of the workstation session archive."""

import base64
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest_bazel
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
    SessionRow,
    make_session_factory,
)
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings


def test_session_archive_requires_owner_and_serves_machine_list() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_id, child_id, version_id, payload_id = (uuid4() for _ in range(4))
    captured_at = datetime(2026, 1, 1, tzinfo=UTC)
    raw_line = b'{"broken":\xff}\n'
    with make_session_factory(engine)() as db:
        db.add(MachineRow(id="fake-host", display_name="Fake host"))
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
    assert machines.json() == {"data": [{"id": "fake-host", "display_name": "Fake host"}]}
    assert client.get("/api/sessions", params={"cursor": "invalid!"}).status_code == 400
    assert client.get(f"/api/sessions/{session_id}").json()["id"] == str(session_id)
    children = client.get("/api/sessions", params={"parent_session_id": str(session_id)}).json()
    assert [child["id"] for child in children["data"]] == [str(child_id)]
    versions = client.get(f"/api/sessions/{session_id}/versions").json()["data"]
    assert [version["id"] for version in versions] == [str(version_id)]
    events = client.get(f"/api/sessions/{session_id}/versions/{version_id}/events").json()["data"]
    assert base64.b64decode(events[0]["raw_base64"]) == raw_line
    assert events[0]["parse_error"] is not None
    assert client.get(f"/api/sessions/{child_id}/versions/{version_id}/events").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}/versions").status_code == 404
    assert client.get(f"/api/sessions/{uuid4()}/versions/{uuid4()}/events").status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
