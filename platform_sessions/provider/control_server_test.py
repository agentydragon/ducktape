"""TLS, bearer, binding, and cleanup tests for the provider control server lifecycle."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
from collections.abc import Callable

import grpc
import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from platform_sessions.provider.control_server import SecureControlServer

_SERVICE = "platform_sessions.provider.v1.SessionProviderControl"
_TOKEN = "synthetic-control-token-with-at-least-32-chars"


def _server_tls_material() -> tuple[bytes, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider-control.test")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


def _register(handler: Callable[[bytes, grpc.aio.ServicerContext], object]) -> Callable[[grpc.aio.Server], None]:
    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler(_SERVICE, {"Probe": grpc.unary_unary_rpc_method_handler(handler)}),)
        )

    return register


def _server(
    token_file,
    certificate: bytes,
    private_key: bytes,
    *,
    handler: Callable[[bytes, grpc.aio.ServicerContext], object],
    bind_address: str = "127.0.0.1:0",
    shutdown_grace_seconds: float = 0.5,
    shutdown_timeout_seconds: float = 1.0,
) -> SecureControlServer:
    return SecureControlServer(
        bind_address=bind_address,
        token_file=token_file,
        certificate_chain_pem=certificate,
        private_key_pem=private_key,
        register_services=_register(handler),
        startup_timeout_seconds=1.0,
        shutdown_grace_seconds=shutdown_grace_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


async def test_secure_server_requires_tls_and_bearer_and_drains_rpc_on_stop(tmp_path) -> None:
    certificate, private_key = _server_tls_material()
    token_file = tmp_path / "control-token"
    token_file.write_text(_TOKEN, encoding="ascii")
    entered = asyncio.Event()
    release = asyncio.Event()
    received: list[bytes] = []

    async def handle(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        del context
        if request == b"drain":
            entered.set()
            await release.wait()
        received.append(request)
        return b"ok:" + request

    server = _server(token_file, certificate, private_key, handler=handle)
    with pytest.raises(RuntimeError, match="has not started"):
        _ = server.target

    await server.start()
    channel = grpc.aio.secure_channel(server.target, grpc.ssl_channel_credentials(root_certificates=certificate))
    call: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(f"/{_SERVICE}/Probe")
    try:
        assert await call(b"allowed", metadata=(("authorization", f"Bearer {_TOKEN}"),), timeout=2) == b"ok:allowed"

        for metadata in ((), (("authorization", "Bearer wrong"),)):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await call(b"denied", metadata=metadata, timeout=2)
            assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED

        async def drain_call() -> bytes:
            return await call(b"drain", metadata=(("authorization", f"Bearer {_TOKEN}"),), timeout=2)

        draining = asyncio.create_task(drain_call())
        await asyncio.wait_for(entered.wait(), timeout=1)
        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0)
        release.set()
        # This proves RPC draining only. Provider-operation serialization and durable recovery
        # across process exit remain the caller's responsibility.
        assert await draining == b"ok:drain"
        await asyncio.wait_for(stopping, timeout=2)
        await server.stop()  # stopping an already stopped server is safe

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await call(b"after-stop", metadata=(("authorization", f"Bearer {_TOKEN}"),), timeout=1)
        assert error.value.code() == grpc.StatusCode.UNAVAILABLE
        assert received == [b"allowed", b"drain"]
    finally:
        release.set()
        await channel.close()
        await server.stop()


async def test_secure_server_rejects_plaintext_and_untrusted_certificates(tmp_path) -> None:
    certificate, private_key = _server_tls_material()
    wrong_root, _wrong_key = _server_tls_material()
    token_file = tmp_path / "control-token"
    token_file.write_text(_TOKEN, encoding="ascii")

    async def handle(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        del context
        return request

    async with _server(token_file, certificate, private_key, handler=handle) as server:
        plaintext = grpc.aio.insecure_channel(server.target)
        untrusted = grpc.aio.secure_channel(server.target, grpc.ssl_channel_credentials(root_certificates=wrong_root))
        try:
            call_plaintext: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = plaintext.unary_unary(f"/{_SERVICE}/Probe")
            with pytest.raises(grpc.aio.AioRpcError):
                await call_plaintext(b"request", metadata=(("authorization", f"Bearer {_TOKEN}"),), timeout=1)

            call_untrusted: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = untrusted.unary_unary(f"/{_SERVICE}/Probe")
            with pytest.raises(grpc.aio.AioRpcError):
                await call_untrusted(b"request", metadata=(("authorization", f"Bearer {_TOKEN}"),), timeout=1)
        finally:
            await plaintext.close()
            await untrusted.close()


async def test_secure_server_rejects_bind_conflicts(tmp_path) -> None:
    certificate, private_key = _server_tls_material()
    token_file = tmp_path / "control-token"
    token_file.write_text(_TOKEN, encoding="ascii")

    async def handle(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        del context
        return request

    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    reservation.listen()
    address = f"127.0.0.1:{reservation.getsockname()[1]}"
    server = _server(token_file, certificate, private_key, handler=handle, bind_address=address)
    try:
        with pytest.raises(OSError, match="could not bind TLS"):
            await server.start()
        assert server._state == "stopped"
    finally:
        reservation.close()
        await server.stop()


def test_secure_server_requires_certificate_and_valid_time_bounds(tmp_path) -> None:
    certificate, private_key = _server_tls_material()
    token_file = tmp_path / "control-token"

    async def handle(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        del context
        return request

    with pytest.raises(ValueError, match="TLS certificate and private key"):
        _server(token_file, b"", private_key, handler=handle)
    with pytest.raises(ValueError, match="shutdown timeout must exceed"):
        SecureControlServer(
            bind_address="127.0.0.1:0",
            token_file=token_file,
            certificate_chain_pem=certificate,
            private_key_pem=private_key,
            register_services=_register(handle),
            shutdown_grace_seconds=1,
            shutdown_timeout_seconds=1,
        )


if __name__ == "__main__":
    pytest_bazel.main()
