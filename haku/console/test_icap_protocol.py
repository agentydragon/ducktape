"""Wire-format tests, using request bytes shaped like the ones Squid 7.6 actually sent.

The spike in ``cluster/k8s/x/squid-egress-spike/`` logged real REQMOD transactions from the Squid
this will run behind; the fixtures below reproduce their shape — ``Encapsulated`` with and without a
body, ``Allow: 204``, ``X-Client-IP``, and Squid's habit of declining a preview offer.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel

from haku.console.icap_protocol import (
    Forward,
    Headers,
    HttpMessage,
    IcapProtocolError,
    Modify,
    OptionsAnnouncement,
    OptionsRequest,
    ReqmodRequest,
    Respond,
    parse_encapsulated,
    parse_headers,
    read_preview_remainder,
    read_request,
    serialise_adaptation,
)

ISTAG = "console-1"


def reader_for(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


def reqmod_bytes(*, http: bytes, encapsulated: str, extra_icap: bytes = b"", body: bytes | None = None) -> bytes:
    head = (
        b"REQMOD icap://console.haku-console.svc:1344/reqmod ICAP/1.0\r\n"
        b"Host: console.haku-console.svc:1344\r\n"
        b"Allow: 204\r\n"
        b"X-Client-IP: 10.244.8.246\r\n" + extra_icap + f"Encapsulated: {encapsulated}\r\n".encode() + b"\r\n"
    )
    return head + http + (body if body is not None else b"")


GET_HTTP = (
    b"GET /v1/messages HTTP/1.1\r\n"
    b"Host: api.anthropic.com\r\n"
    b"Authorization: Bearer placeholder-token\r\n"
    b"Accept: */*\r\n"
    b"\r\n"
)


async def test_reqmod_without_body_parses_headers_and_client_ip():
    request = await read_request(reader_for(reqmod_bytes(http=GET_HTTP, encapsulated="req-hdr=0, null-body=118")))

    assert isinstance(request, ReqmodRequest)
    assert request.http.start_line == b"GET /v1/messages HTTP/1.1"
    assert request.http.headers.get("Authorization") == "Bearer placeholder-token"
    assert request.http.body == b""
    assert request.allows_204
    # Squid asserts this; the agent cannot forge it, which is what makes it usable as identity.
    assert request.client_ip == "10.244.8.246"


async def test_reqmod_with_chunked_body_is_decoded_whole():
    post = (
        b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: 11\r\n\r\n",
        b"b\r\nhello world\r\n0\r\n\r\n",
    )
    request = await read_request(
        reader_for(reqmod_bytes(http=post[0], encapsulated="req-hdr=0, req-body=76", body=post[1]))
    )

    assert isinstance(request, ReqmodRequest)
    assert request.http.body == b"hello world"
    # No Preview header: the body that arrived is the whole body, so no continue round trip is owed.
    assert request.body_complete


async def test_preview_without_ieof_leaves_the_body_incomplete():
    request = await read_request(
        reader_for(
            reqmod_bytes(
                http=b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
                encapsulated="req-hdr=0, req-body=60",
                extra_icap=b"Preview: 4\r\n",
                body=b"4\r\nabcd\r\n0\r\n\r\n",
            )
        )
    )

    assert isinstance(request, ReqmodRequest)
    assert request.preview == 4
    assert not request.body_complete
    assert request.http.body == b"abcd"


async def test_preview_terminated_by_ieof_is_already_complete():
    request = await read_request(
        reader_for(
            reqmod_bytes(
                http=b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
                encapsulated="req-hdr=0, req-body=60",
                extra_icap=b"Preview: 4\r\n",
                body=b"4\r\nabcd\r\n0; ieof\r\n\r\n",
            )
        )
    )

    assert isinstance(request, ReqmodRequest)
    assert request.body_complete
    # Asking for a remainder that Squid already said does not exist would hang until icap_io_timeout.
    with pytest.raises(IcapProtocolError):
        await read_preview_remainder(reader_for(b""), request)


async def test_preview_remainder_appends_to_the_previewed_bytes():
    request = await read_request(
        reader_for(
            reqmod_bytes(
                http=b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
                encapsulated="req-hdr=0, req-body=60",
                extra_icap=b"Preview: 4\r\n",
                body=b"4\r\nabcd\r\n0\r\n\r\n",
            )
        )
    )
    assert isinstance(request, ReqmodRequest)

    completed = await read_preview_remainder(reader_for(b"3\r\nefg\r\n0\r\n\r\n"), request)

    assert completed.http.body == b"abcdefg"
    assert completed.body_complete


async def test_options_is_recognised():
    payload = b"OPTIONS icap://console/reqmod ICAP/1.0\r\nHost: console\r\n\r\n"
    assert isinstance(await read_request(reader_for(payload)), OptionsRequest)


async def test_end_of_stream_between_transactions_is_not_an_error():
    assert await read_request(reader_for(b"")) is None


async def test_truncated_header_block_raises():
    with pytest.raises(IcapProtocolError):
        await read_request(reader_for(b"REQMOD icap://console/reqmod ICAP/1.0\r\nHost: console\r\n"))


async def test_reqmod_without_encapsulated_raises():
    payload = b"REQMOD icap://console/reqmod ICAP/1.0\r\nHost: console\r\n\r\n"
    with pytest.raises(IcapProtocolError):
        await read_request(reader_for(payload))


async def test_unsupported_method_raises():
    payload = b"RESPMOD icap://console/respmod ICAP/1.0\r\nHost: console\r\n\r\n"
    with pytest.raises(IcapProtocolError):
        await read_request(reader_for(payload))


def test_duplicate_headers_survive_parsing():
    headers = parse_headers(b"Authorization: one\r\nAuthorization: two\r\n")

    assert len(headers.pairs) == 2
    # A fence that collapsed these would rule on a request the origin never receives.
    assert headers.get("Authorization") == "one"


def test_obs_fold_is_rejected_rather_than_joined():
    with pytest.raises(IcapProtocolError):
        parse_headers(b"Authorization: Bearer\r\n\tcontinued\r\n")


def test_header_lookup_is_case_insensitive():
    assert parse_headers(b"X-Client-IP: 10.0.0.1\r\n").get("x-client-ip") == "10.0.0.1"


def test_parse_encapsulated_rejects_a_missing_offset():
    with pytest.raises(IcapProtocolError):
        parse_encapsulated("req-hdr=0, req-body")


def test_parse_encapsulated_reads_offsets():
    assert parse_encapsulated("req-hdr=0, req-body=412") == {"req-hdr": 0, "req-body": 412}


def reqmod_for(headers: Headers, *, allow_204: bool = True) -> ReqmodRequest:
    icap = Headers((("Allow", "204"),) if allow_204 else ())
    return ReqmodRequest(icap, HttpMessage(b"GET /v1/messages HTTP/1.1", headers))


def test_forward_with_allow_204_sends_no_content():
    request = reqmod_for(Headers((("Authorization", "Bearer placeholder"),)))

    payload = serialise_adaptation(Forward(), request, istag=ISTAG).payload

    assert payload.startswith(b"ICAP/1.0 204 No Content\r\n")
    assert b'ISTag: "console-1"' in payload


def test_forward_without_allow_204_echoes_the_unmodified_request():
    request = reqmod_for(Headers((("Authorization", "Bearer placeholder"),)), allow_204=False)

    payload = serialise_adaptation(Forward(), request, istag=ISTAG).payload

    assert payload.startswith(b"ICAP/1.0 200 OK\r\n")
    assert b"Authorization: Bearer placeholder" in payload


def test_modify_replaces_the_credential_on_the_wire():
    request = reqmod_for(Headers((("Authorization", "Bearer placeholder"), ("Accept", "*/*"))))
    rewritten = request.http.headers.replacing("Authorization", "Bearer real-secret")

    payload = serialise_adaptation(
        Modify(HttpMessage(request.http.start_line, rewritten)), request, istag=ISTAG
    ).payload

    assert b"Authorization: Bearer real-secret" in payload
    assert b"Bearer placeholder" not in payload
    assert b"Accept: */*" in payload
    assert b"Encapsulated: req-hdr=0, null-body=" in payload


def test_modify_with_a_body_declares_req_body_at_the_head_length():
    http = HttpMessage(b"POST /v1/messages HTTP/1.1", Headers((("Host", "api.anthropic.com"),)), b"hello")

    payload = serialise_adaptation(Modify(http), reqmod_for(Headers()), istag=ISTAG).payload

    head_length = len(http.serialise_head())
    assert f"Encapsulated: req-hdr=0, req-body={head_length}".encode() in payload
    assert payload.endswith(b"5\r\nhello\r\n0\r\n\r\n")


def test_respond_encapsulates_a_response_rather_than_a_request():
    blocked = Respond(b"HTTP/1.1 403 Forbidden", Headers((("Content-Type", "text/plain"),)), b"denied by policy\n")

    payload = serialise_adaptation(blocked, reqmod_for(Headers()), istag=ISTAG).payload

    assert b"Encapsulated: res-hdr=0, res-body=" in payload
    assert b"HTTP/1.1 403 Forbidden" in payload
    assert b"denied by policy\n" in payload


def test_options_announcement_advertises_what_the_server_implements():
    payload = OptionsAnnouncement(istag=ISTAG, service="haku-console").serialise()

    assert b"Methods: REQMOD" in payload
    assert b"Allow: 204" in payload
    assert b"Preview: 0" in payload
    # Quoted per RFC 3507 §4.7; Squid compares the token literally, so quoting must not vary.
    assert b'ISTag: "console-1"' in payload


if __name__ == "__main__":
    pytest_bazel.main()
