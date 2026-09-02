"""Stub ICAP REQMOD service for the Squid egress spike.

Answers the Squid-side questions about ICAP *before* haku-console grows an ICAP
server, so that if REQMOD cannot do what the design needs, no console code was
written first. The contract this establishes -- what Squid sends, what it accepts
back, how it behaves when we misbehave -- transfers unchanged to the real thing.

Deliberately stdlib-only: the spike's CiliumNetworkPolicy permits egress to its
own namespace and DNS, so there is no `pip install pyicap` at container start.
ICAP is small enough to hand-roll for the subset Squid actually speaks.

Behaviour is selected by an `X-Spike-Mode` header on the *encapsulated* request,
so one deployment covers every case and `curl` chooses which:

    rewrite (default)  replace Authorization, return the modified request
    passthrough        return 204 No Content (no modification)
    block              return an encapsulated HTTP 403 instead of a request
    slow               sleep past the client timeout, then rewrite
    crash              close the connection mid-transaction

Every transaction is logged with what arrived, which is the point: the questions
are "does REQMOD see the bumped plaintext", "does the POST body transit", and
"what does Squid do when we fail".
"""

from __future__ import annotations

import os
import socketserver
import sys
import time

ISTAG = '"spike-icap-1"'
LISTEN_PORT = int(os.environ.get("ICAP_PORT", "1344"))
SLOW_SECONDS = int(os.environ.get("ICAP_SLOW_SECONDS", "30"))
INJECTED = os.environ.get("ICAP_INJECTED_CREDENTIAL", "Bearer icap-injected-do-not-use")


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_headers(raw: bytes) -> list[tuple[str, str]]:
    """Header lines to (name, value) pairs, order preserved, no folding."""
    out = []
    for line in raw.split(b"\r\n"):
        if not line or b":" not in line:
            continue
        name, _, value = line.partition(b":")
        out.append((name.decode("latin-1").strip(), value.decode("latin-1").strip()))
    return out


def header(pairs: list[tuple[str, str]], name: str) -> str | None:
    lowered = name.lower()
    return next((v for k, v in pairs if k.lower() == lowered), None)


def parse_encapsulated(value: str) -> list[tuple[str, int]]:
    """`req-hdr=0, req-body=412` -> [("req-hdr", 0), ("req-body", 412)]."""
    parts = []
    for item in value.split(","):
        key, _, offset = item.strip().partition("=")
        parts.append((key.strip(), int(offset)))
    return parts


def read_chunked(rfile) -> tuple[bytes, bool]:
    """Read a chunked body. Returns (body, saw_ieof).

    `ieof` marks the end of a *preview* whose body was complete -- Squid says
    "that is all there was", so asking for more would hang.
    """
    body = b""
    saw_ieof = False
    while True:
        size_line = rfile.readline()
        if not size_line:
            break
        head, _, ext = size_line.strip().partition(b";")
        if b"ieof" in ext:
            saw_ieof = True
        try:
            size = int(head, 16)
        except ValueError:
            break
        if size == 0:
            rfile.readline()  # trailing CRLF of the terminating chunk
            break
        body += rfile.read(size)
        rfile.readline()  # CRLF after each chunk
    return body, saw_ieof


def as_chunked(body: bytes) -> bytes:
    if not body:
        return b"0\r\n\r\n"
    return b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)


def rebuild_request(start_line: bytes, pairs: list[tuple[str, str]]) -> bytes:
    lines = [start_line] + [f"{k}: {v}".encode("latin-1") for k, v in pairs]
    return b"\r\n".join(lines) + b"\r\n\r\n"


def fingerprint(value: str | None) -> str:
    """Enough to tell two credentials apart, not enough to redeem either.

    The spike's credentials are fake, but this file is the shape the real
    service takes, and there the whole question is whether the ICAP service
    sees a live secret -- answering it must not itself put one in a log or a
    header bound for the origin.
    """
    if value is None:
        return "absent"
    return f"{value[:12]}...len={len(value)}"


def observations(
    icap_headers: list[tuple[str, str]],
    pairs: list[tuple[str, str]],
    encapsulated: str,
    preview: str | None,
    body: bytes,
    saw_ieof: bool,
) -> list[tuple[str, str]]:
    """What the service saw, as headers the echo origin will reflect back.

    The spike has no way to read this pod's log: pods/log is refused in this
    namespace and squid-egress-spike is not in the loki-read-proxy allowlist.
    Reflecting through the origin is the same trick echo-origin already exists
    for, and it keeps the observation on the same request it describes.

    X-Icap-Saw-Spike-Client is the load-bearing one. squid.conf adds
    X-Spike-Client via request_header_add; if it is already present here then
    Squid's own header rewriting -- including the credential substitution in
    credentials.conf -- runs BEFORE adaptation, which means the ICAP service is
    inside the secret path. Absent means REQMOD runs first and the service
    never sees the real credential.
    """
    return [
        ("X-Icap-Saw-Authorization", fingerprint(header(pairs, "Authorization"))),
        ("X-Icap-Saw-Spike-Client", header(pairs, "X-Spike-Client") or "absent"),
        ("X-Icap-Saw-Via", header(pairs, "Via") or "absent"),
        ("X-Icap-Saw-Client-Ip", header(icap_headers, "X-Client-IP") or "absent"),
        ("X-Icap-Saw-Client-Username", header(icap_headers, "X-Client-Username") or "absent"),
        ("X-Icap-Saw-Encapsulated", encapsulated.replace(",", ";")),
        ("X-Icap-Saw-Preview", "absent" if preview is None else preview),
        ("X-Icap-Saw-Body", f"bytes={len(body)} ieof={saw_ieof}"),
    ]


class Handler(socketserver.StreamRequestHandler):
    # Squid keeps ICAP connections open; a per-transaction timeout stops a stuck
    # peer from pinning a thread forever.
    timeout = 120

    def handle(self) -> None:
        peer = self.client_address[0]
        while True:
            request_line = self.rfile.readline()
            if not request_line:
                return
            method = request_line.split(b" ", 1)[0].decode("latin-1")
            raw = b""
            while True:
                line = self.rfile.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                raw += line
            icap_headers = parse_headers(raw)

            log(f"\n=== {method} from {peer} ===")
            for k, v in icap_headers:
                log(f"  icap> {k}: {v}")

            if method == "OPTIONS":
                self.send_options()
                continue
            if method == "REQMOD":
                if not self.handle_reqmod(icap_headers):
                    return
                continue
            log(f"  !! unsupported ICAP method {method}")
            self.wfile.write(b"ICAP/1.0 405 Method Not Allowed\r\n\r\n")
            return

    def send_options(self) -> None:
        # Preview: 0 asks Squid to send headers plus a zero-length body preview,
        # which is how we find out whether a body can be avoided when the service
        # only wants to rewrite a header.
        body = (
            b"ICAP/1.0 200 OK\r\n"
            b"Methods: REQMOD\r\n"
            b"Service: squid-egress-spike stub\r\n"
            b"ISTag: " + ISTAG.encode() + b"\r\n"
            b"Allow: 204\r\n"
            b"Preview: 0\r\n"
            b"Options-TTL: 60\r\n"
            b"Encapsulated: null-body=0\r\n"
            b"\r\n"
        )
        self.wfile.write(body)
        log("  <icap OPTIONS 200 (Methods: REQMOD, Allow: 204, Preview: 0)")

    def handle_reqmod(self, icap_headers: list[tuple[str, str]]) -> bool:
        """Returns False if the connection should be dropped."""
        encapsulated = header(icap_headers, "Encapsulated") or "req-hdr=0, null-body=0"
        sections = parse_encapsulated(encapsulated)
        offsets = dict(sections)
        preview = header(icap_headers, "Preview")
        allow = header(icap_headers, "Allow") or ""

        # The encapsulated HTTP request head, terminated by a blank line.
        head = b""
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            head += line
        start_line, _, rest = head.partition(b"\r\n")
        pairs = parse_headers(rest)

        log(f"  http> {start_line.decode('latin-1')}")
        for k, v in pairs:
            shown = v if k.lower() != "authorization" else f"{v[:12]}...(len {len(v)})"
            log(f"  http> {k}: {shown}")

        body = b""
        saw_ieof = False
        if "req-body" in offsets:
            body, saw_ieof = read_chunked(self.rfile)
            log(f"  body: {len(body)} bytes received (preview={preview}, ieof={saw_ieof})")
        else:
            log(f"  body: none (null-body; preview={preview})")

        mode = (header(pairs, "X-Spike-Mode") or "rewrite").lower()
        log(f"  mode: {mode}")

        if mode == "crash":
            log("  <icap (closing connection without responding)")
            return False

        if mode == "slow":
            log(f"  ... sleeping {SLOW_SECONDS}s before answering")
            time.sleep(SLOW_SECONDS)

        if mode == "passthrough":
            if "204" in allow:
                self.wfile.write(b"ICAP/1.0 204 No Content\r\nISTag: " + ISTAG.encode() + b"\r\n\r\n")
                log("  <icap 204 No Content (unmodified)")
                return True
            log("  !! 204 not allowed by client; falling through to an unmodified 200")

        if mode == "block":
            page = b"blocked by the spike ICAP service\n"
            res_head = (
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(page)).encode() + b"\r\n"
                b"\r\n"
            )
            self.wfile.write(
                b"ICAP/1.0 200 OK\r\n"
                b"ISTag: " + ISTAG.encode() + b"\r\n"
                b"Encapsulated: res-hdr=0, res-body=" + str(len(res_head)).encode() + b"\r\n"
                b"\r\n" + res_head + as_chunked(page)
            )
            log("  <icap 200 with encapsulated HTTP 403 (block)")
            return True

        # rewrite (default): swap Authorization, hand the request back.
        #
        # If this arrived as a preview whose body was NOT complete, the rest is
        # still on the wire. ICAP has no way to say "modify the head, keep the
        # body, and do not send it to me" -- so a service that rewrites a header
        # must take delivery of the whole body. That is the finding, not a
        # limitation of this stub.
        if preview is not None and "req-body" in offsets and not saw_ieof:
            self.wfile.write(b"ICAP/1.0 100 Continue\r\n\r\n")
            log("  <icap 100 Continue (asking for the rest of the body)")
            more, _ = read_chunked(self.rfile)
            body += more
            log(f"  body: {len(body)} bytes total after continue")

        had_auth = header(pairs, "Authorization")
        saw = observations(icap_headers, pairs, encapsulated, preview, body, saw_ieof)
        pairs = [(k, v) for k, v in pairs if k.lower() != "authorization"]
        pairs.append(("Authorization", INJECTED))
        pairs.append(("X-Icap-Stub", "rewrote-authorization"))
        pairs.extend(saw)
        new_head = rebuild_request(start_line, pairs)

        if body:
            self.wfile.write(
                b"ICAP/1.0 200 OK\r\n"
                b"ISTag: " + ISTAG.encode() + b"\r\n"
                b"Encapsulated: req-hdr=0, req-body=" + str(len(new_head)).encode() + b"\r\n"
                b"\r\n" + new_head + as_chunked(body)
            )
        else:
            self.wfile.write(
                b"ICAP/1.0 200 OK\r\n"
                b"ISTag: " + ISTAG.encode() + b"\r\n"
                b"Encapsulated: req-hdr=0, null-body=" + str(len(new_head)).encode() + b"\r\n"
                b"\r\n" + new_head
            )
        log(f"  <icap 200 modified (Authorization present before: {had_auth is not None})")
        return True


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    log(f"stub ICAP service listening on 0.0.0.0:{LISTEN_PORT}")
    with Server(("0.0.0.0", LISTEN_PORT), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("shutting down")
            sys.exit(0)


if __name__ == "__main__":
    main()
