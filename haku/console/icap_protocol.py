"""ICAP/1.0 (RFC 3507) wire format — the subset Squid actually puts on the wire.

Parsing and serialisation only: no sockets, no policy, no credentials. The server in
``icap_server.py`` owns the connection and calls into here, so the protocol can be tested by
feeding an ``asyncio.StreamReader`` and reading back bytes.

Scope is deliberately the measured subset rather than the whole RFC. The spike in
``cluster/k8s/x/squid-egress-spike/`` ran a stub service behind the Squid 7.6 this will sit behind
and recorded what arrived: ``OPTIONS``, ``REQMOD``, chunked bodies, ``204 No Content``, and the
``100 Continue`` preview handshake. RESPMOD never appears because ``adaptation_access`` only routes
REQMOD; trailers never appear; and Squid ignored a ``Preview: 0`` offer outright and encapsulated
the full body. Preview handling is implemented anyway because we advertise it and a differently
configured Squid may take it up — but it is the one path here no live proxy has exercised.

Findings that shaped this file are in <cluster/docs/plans/agent_egress_proxy_options.md>.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

ICAP_VERSION = b"ICAP/1.0"

# RFC 3507 §4.4. `req-hdr`/`res-hdr` carry byte offsets into the encapsulated message; the -body
# entries name where a chunked body starts; `null-body` names the end of a message that has none.
NULL_BODY = "null-body"
REQ_HDR = "req-hdr"
REQ_BODY = "req-body"
RES_HDR = "res-hdr"
RES_BODY = "res-body"


class IcapProtocolError(Exception):
    """The peer sent something this parser cannot make sense of.

    Raised rather than defaulted around: a proxy that fails to parse a fence's traffic must drop the
    transaction, and Squid renders that as ERR_ICAP_FAILURE, which is the fail-closed outcome.
    """


@dataclass(frozen=True, slots=True)
class Headers:
    """Header lines in arrival order, duplicates preserved.

    Not a dict: ICAP and HTTP both allow repeats, and a fence that silently collapsed two
    ``Authorization`` headers into one would be deciding policy on a request the origin never sees.
    """

    pairs: tuple[tuple[str, str], ...] = ()

    def get(self, name: str) -> str | None:
        """First value for ``name``, case-insensitively; ``None`` when absent."""
        lowered = name.lower()
        return next((value for key, value in self.pairs if key.lower() == lowered), None)

    def without(self, name: str) -> Headers:
        lowered = name.lower()
        return Headers(tuple((key, value) for key, value in self.pairs if key.lower() != lowered))

    def with_appended(self, *added: tuple[str, str]) -> Headers:
        return Headers(self.pairs + added)

    def replacing(self, name: str, value: str) -> Headers:
        return self.without(name).with_appended((name, value))


@dataclass(frozen=True, slots=True)
class HttpMessage:
    """An encapsulated HTTP request or response: its start line, headers, and body.

    ``body`` is the fully decoded entity. Squid delivers it chunked; nothing downstream of the
    parser wants to think in chunks, and the bodies here are single requests rather than streams.
    """

    start_line: bytes
    headers: Headers
    body: bytes = b""

    def serialise_head(self) -> bytes:
        lines = [self.start_line, *(f"{key}: {value}".encode("latin-1") for key, value in self.headers.pairs)]
        return b"\r\n".join(lines) + b"\r\n\r\n"


@dataclass(frozen=True, slots=True)
class OptionsRequest:
    headers: Headers


@dataclass(frozen=True, slots=True)
class ReqmodRequest:
    """A REQMOD transaction as it arrived.

    ``icap_headers`` are the ICAP-level ones — where ``X-Client-IP`` lives when
    ``icap_send_client_ip`` is on, which is how the caller is identified. It is asserted by Squid
    rather than by the client, so unlike anything in ``http.headers`` the agent cannot forge it.
    """

    icap_headers: Headers
    http: HttpMessage
    preview: int | None = None
    body_complete: bool = True

    @property
    def allows_204(self) -> bool:
        allow = self.icap_headers.get("Allow") or ""
        return "204" in allow

    @property
    def client_ip(self) -> str | None:
        return self.icap_headers.get("X-Client-IP")


type IcapRequest = OptionsRequest | ReqmodRequest


@dataclass(frozen=True, slots=True)
class Forward:
    """Send the request on untouched (ICAP 204, or an unmodified 200 when 204 is not allowed)."""


@dataclass(frozen=True, slots=True)
class Modify:
    """Send this request instead of the one that arrived."""

    http: HttpMessage


@dataclass(frozen=True, slots=True)
class Respond:
    """Answer the client directly and never contact the origin."""

    status_line: bytes
    headers: Headers
    body: bytes = b""

    def as_http(self) -> HttpMessage:
        return HttpMessage(self.status_line, self.headers, self.body)


type Adaptation = Forward | Modify | Respond


@dataclass(frozen=True, slots=True)
class OptionsAnnouncement:
    """What this service tells Squid it can do, in reply to OPTIONS.

    ``istag`` is an opaque service-version token. Squid keys any cached adaptation on it, so it must
    change whenever the service's behaviour changes — a stale ISTag means old decisions are reused
    against new policy.
    """

    istag: str
    service: str
    methods: tuple[str, ...] = ("REQMOD",)
    preview: int | None = 0
    options_ttl_seconds: int = 60
    allow_204: bool = True

    def serialise(self) -> bytes:
        lines = [
            ICAP_VERSION + b" 200 OK",
            b"Methods: " + ", ".join(self.methods).encode("latin-1"),
            b"Service: " + self.service.encode("latin-1"),
            b"ISTag: " + _quote_istag(self.istag),
            b"Options-TTL: " + str(self.options_ttl_seconds).encode("latin-1"),
        ]
        if self.allow_204:
            lines.append(b"Allow: 204")
        if self.preview is not None:
            lines.append(b"Preview: " + str(self.preview).encode("latin-1"))
        lines.append(b"Encapsulated: null-body=0")
        return b"\r\n".join(lines) + b"\r\n\r\n"


def _quote_istag(istag: str) -> bytes:
    # RFC 3507 §4.7 makes ISTag a quoted string. Squid tolerates an unquoted one but then compares
    # it literally, so a service that quotes on one path and not another looks like two services.
    return f'"{istag}"'.encode("latin-1")


def parse_headers(raw: bytes) -> Headers:
    """Header block (no terminating blank line) to ordered pairs.

    Obs-folded continuation lines are rejected rather than joined: they are deprecated by RFC 7230,
    Squid does not emit them, and quietly accepting them would make what the policy layer sees
    depend on how an attacker chose to wrap a header.
    """
    pairs = []
    for line in raw.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise IcapProtocolError(f"obs-fold continuation line is not supported: {line!r}")
        name, separator, value = line.partition(b":")
        if not separator:
            raise IcapProtocolError(f"header line has no colon: {line!r}")
        pairs.append((name.decode("latin-1").strip(), value.decode("latin-1").strip()))
    return Headers(tuple(pairs))


def parse_encapsulated(value: str) -> dict[str, int]:
    """``req-hdr=0, req-body=412`` to ``{"req-hdr": 0, "req-body": 412}``."""
    sections = {}
    for item in value.split(","):
        key, separator, offset = item.strip().partition("=")
        if not separator:
            raise IcapProtocolError(f"Encapsulated entry has no offset: {item!r}")
        try:
            sections[key.strip()] = int(offset)
        except ValueError as exc:
            raise IcapProtocolError(f"Encapsulated offset is not an integer: {item!r}") from exc
    return sections


def encapsulated_header(http: HttpMessage, *, head_kind: str, body_kind: str) -> bytes:
    head_length = len(http.serialise_head())
    tail = f"{body_kind}={head_length}" if http.body else f"{NULL_BODY}={head_length}"
    return f"Encapsulated: {head_kind}=0, {tail}".encode("latin-1")


def as_chunked(body: bytes) -> bytes:
    if not body:
        return b"0\r\n\r\n"
    return b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)


@dataclass(frozen=True, slots=True)
class ChunkedBody:
    data: bytes = b""
    # `0; ieof` terminates a preview whose body was complete -- Squid is saying "that was all of
    # it". Asking for more after that hangs the transaction until icap_io_timeout fires.
    ends_preview: bool = False


async def read_chunked(reader: asyncio.StreamReader) -> ChunkedBody:
    chunks: list[bytes] = []
    ends_preview = False
    while True:
        size_line = await reader.readline()
        if not size_line:
            raise IcapProtocolError("connection closed inside a chunked body")
        size_field, _, extension = size_line.strip().partition(b";")
        if b"ieof" in extension:
            ends_preview = True
        try:
            size = int(size_field, 16)
        except ValueError as exc:
            raise IcapProtocolError(f"chunk size is not hexadecimal: {size_line!r}") from exc
        if size == 0:
            await reader.readline()  # CRLF closing the terminating chunk
            return ChunkedBody(b"".join(chunks), ends_preview)
        chunks.append(await reader.readexactly(size))
        await reader.readline()  # CRLF after each chunk


async def _read_head_block(reader: asyncio.StreamReader) -> bytes:
    """Everything up to (not including) the blank line that ends a header block."""
    lines: list[bytes] = []
    while True:
        line = await reader.readline()
        if not line:
            raise IcapProtocolError("connection closed inside a header block")
        if line in (b"\r\n", b"\n"):
            return b"".join(lines)
        lines.append(line)


async def read_request(reader: asyncio.StreamReader) -> IcapRequest | None:
    """Read one ICAP transaction. ``None`` at a clean end of stream between transactions.

    Squid keeps the connection open and pipelines transactions over it, so a closed socket here is
    ordinary rather than an error — but a socket that closes mid-message is not, and raises.
    """
    request_line = await reader.readline()
    if not request_line:
        return None
    method = request_line.split(b" ", 1)[0].decode("latin-1").upper()
    icap_headers = parse_headers(await _read_head_block(reader))

    match method:
        case "OPTIONS":
            return OptionsRequest(icap_headers)
        case "REQMOD":
            return await _read_reqmod(reader, icap_headers)
        case _:
            raise IcapProtocolError(f"unsupported ICAP method {method!r}")


async def _read_reqmod(reader: asyncio.StreamReader, icap_headers: Headers) -> ReqmodRequest:
    encapsulated = icap_headers.get("Encapsulated")
    if encapsulated is None:
        raise IcapProtocolError("REQMOD without an Encapsulated header")
    sections = parse_encapsulated(encapsulated)
    if REQ_HDR not in sections:
        raise IcapProtocolError(f"REQMOD without a {REQ_HDR} section: {encapsulated!r}")

    head = await _read_head_block(reader)
    start_line, _, header_block = head.partition(b"\r\n")
    http = HttpMessage(start_line, parse_headers(header_block))

    raw_preview = icap_headers.get("Preview")
    preview = int(raw_preview) if raw_preview is not None else None

    if REQ_BODY not in sections:
        return ReqmodRequest(icap_headers, http, preview=preview)

    chunked = await read_chunked(reader)
    http = HttpMessage(http.start_line, http.headers, chunked.data)
    # Outside a preview the chunked body is the whole entity. Inside one it is complete only when
    # Squid marked the terminator `ieof`.
    complete = preview is None or chunked.ends_preview
    return ReqmodRequest(icap_headers, http, preview=preview, body_complete=complete)


async def read_preview_remainder(reader: asyncio.StreamReader, request: ReqmodRequest) -> ReqmodRequest:
    """Take delivery of the rest of a previewed body, having sent ``100 Continue``.

    ICAP offers no way to say "I have seen enough of the body, modify the head and forward the rest
    untouched" — a service that returns a modified message owns the whole message. So a header
    rewrite still pulls the entire body across. Measured against Squid 7.6, this path never
    triggers: Squid declined the ``Preview: 0`` offer and sent the full body up front.
    """
    if request.body_complete:
        raise IcapProtocolError("preview remainder requested for an already-complete body")
    remainder = await read_chunked(reader)
    http = HttpMessage(request.http.start_line, request.http.headers, request.http.body + remainder.data)
    return ReqmodRequest(request.icap_headers, http, preview=request.preview, body_complete=True)


@dataclass(frozen=True, slots=True)
class IcapResponse:
    """A serialised ICAP reply plus whether the connection may carry another transaction."""

    payload: bytes
    keep_alive: bool = True


CONTINUE_100 = IcapResponse(ICAP_VERSION + b" 100 Continue\r\n\r\n")


def _serialise_200(http: HttpMessage, *, istag: str, head_kind: str, body_kind: str) -> IcapResponse:
    return IcapResponse(
        ICAP_VERSION
        + b" 200 OK\r\n"
        + b"ISTag: "
        + _quote_istag(istag)
        + b"\r\n"
        + encapsulated_header(http, head_kind=head_kind, body_kind=body_kind)
        + b"\r\n\r\n"
        + http.serialise_head()
        + (as_chunked(http.body) if http.body else b"")
    )


def serialise_adaptation(adaptation: Adaptation, request: ReqmodRequest, *, istag: str) -> IcapResponse:
    """Render an adaptation decision as an ICAP reply.

    ``Forward`` becomes a 204 only when the client allowed one. Otherwise RFC 3507 requires echoing
    the unmodified request back as a 200, which is why the original is a parameter here: a Squid
    that did not advertise ``Allow: 204`` must still be answerable without inventing a body.
    """
    if isinstance(adaptation, Forward):
        if request.allows_204:
            return IcapResponse(ICAP_VERSION + b" 204 No Content\r\nISTag: " + _quote_istag(istag) + b"\r\n\r\n")
        return _serialise_200(request.http, istag=istag, head_kind=REQ_HDR, body_kind=REQ_BODY)
    if isinstance(adaptation, Modify):
        return _serialise_200(adaptation.http, istag=istag, head_kind=REQ_HDR, body_kind=REQ_BODY)
    return _serialise_200(adaptation.as_http(), istag=istag, head_kind=RES_HDR, body_kind=RES_BODY)
