from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from devinfra.claude.session_export.conftest import (
    RESUME_TOKEN,
    TEST_ACCESS_TOKEN,
    FakeSessionsService,
    SseConnection,
    make_credential,
    make_events,
)
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.probe import VARIANTS, probe

ONE = "session_test0001"


def credential_store(tmp_path: Path, *, expires_in: timedelta = timedelta(hours=1)) -> CredentialStore:
    store = CredentialStore(tmp_path / "credential.json")
    store.save(make_credential(expires_in=expires_in))
    return store


async def test_the_probe_reports_each_variant_with_its_status_and_reveals_no_secret(
    service: FakeSessionsService, tmp_path: Path
) -> None:
    service.events = {ONE: make_events(3)}

    def script(stream: SseConnection) -> None:  # the first stream to open is the first variant that is let in
        stream.send(None, {"keepalive": True})
        stream.send("added", {"id": "cse_x", "title": "a private title"})
        stream.send("changed", {"id": "cse_x", "title": "a private title"}, frame_id="cursor-1")
        stream.send("changed", {"id": "cse_x", "title": "a private title"}, frame_id="cursor-1")

    service.on_open = [script, SseConnection.close]  # the second variant let in is ended by the server
    lines: list[str] = []
    await probe(
        credential_store(tmp_path),
        session_id=ONE,
        listen_seconds=0.2,
        out=lines.append,
        transport=httpx.MockTransport(service.handle),
    )

    assert lines[0] == "200 list of one session"
    statuses = [line.split()[0] for line in lines[1 : 1 + len(VARIANTS)]]
    # The gate is the platform header: only the variants that send it, and the routes that have no gate, open.
    assert statuses == ["404", "404", "200", "200", "404", "404", "200", "200"]
    assert lines[1].startswith("404 watch as the sync sends it: ")
    assert "endpoint not enabled" in lines[1]
    [first_stream] = [line for line in lines if "4 frame(s)" in line]
    assert "1 id(s) sent again, open at the end" in first_stream
    assert "[added x1: keys ['id', 'title']]" in first_stream
    assert "[changed x2: keys ['id', 'title']]" in first_stream
    assert any("0 frame(s)" in line and "closed by the server" in line for line in lines)
    assert lines[-1].startswith("200 event stream of session_test0001 from sequence_num 3: ")
    output = "\n".join(lines)
    assert TEST_ACCESS_TOKEN not in output
    assert RESUME_TOKEN not in output
    assert "a private title" not in output


async def test_the_probe_stops_rather_than_refresh_a_lapsed_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lapsed"):
        await probe(credential_store(tmp_path, expires_in=timedelta(seconds=-1)), session_id=None, listen_seconds=0.1)


if __name__ == "__main__":
    pytest_bazel.main()
