from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest
import pytest_bazel
from more_itertools import one

from devinfra.claude.session_export.conftest import (
    ONE,
    RESUME_TOKEN,
    TEST_ACCESS_TOKEN,
    FakeSessionsService,
    SseConnection,
    make_events,
)
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.probe import CLIENT_PLATFORM, VARIANTS, Variant, probe


def line_for(lines: list[str], variant: Variant) -> str:
    """The probe's line for `variant`, `<status> <label>: <what came back>`; there must be exactly one."""
    return one(line for line in lines if line.partition(" ")[2].startswith(f"{variant.label}: "))


async def test_the_probe_reports_each_variant_with_its_status_and_reveals_no_secret(
    service: FakeSessionsService, credential_store: Callable[..., CredentialStore]
) -> None:
    service.events = {ONE: make_events(3)}

    def script(stream: SseConnection) -> None:  # the first stream to open is the first variant that is let in
        stream.send(None, {"keepalive": True})
        stream.send("added", {"id": "cse_x", "title": "a private title"})

    service.on_open = [script]
    lines: list[str] = []
    await probe(
        credential_store(),
        session_id=ONE,
        listen_seconds=0.2,
        out=lines.append,
        transport=httpx.MockTransport(service.handle),
    )

    assert lines[0] == "200 list of one session"
    assert all(line_for(lines, variant)[:3].isdigit() for variant in VARIANTS)  # each variant got an HTTP status
    # The watch is gated on the platform header: refused as the sync sends it, let in once that header is added.
    as_the_sync_sends_it = line_for(lines, VARIANTS[0])
    assert as_the_sync_sends_it.startswith("404 ")
    assert "endpoint not enabled" in as_the_sync_sends_it
    assert line_for(lines, one(v for v in VARIANTS if v.headers == CLIENT_PLATFORM)).startswith("200 ")
    assert any("[added: keys ['id', 'title']]" in line for line in lines)
    assert lines[-1].startswith("200 event stream of session_test0001 from sequence_num 3: ")
    output = "\n".join(lines)
    assert TEST_ACCESS_TOKEN not in output
    assert RESUME_TOKEN not in output
    assert "a private title" not in output


async def test_the_probe_stops_rather_than_refresh_a_lapsed_token(
    credential_store: Callable[..., CredentialStore],
) -> None:
    with pytest.raises(ValueError, match="lapsed"):
        await probe(credential_store(expires_in=timedelta(seconds=-1)), session_id=None, listen_seconds=0.1)


if __name__ == "__main__":
    pytest_bazel.main()
