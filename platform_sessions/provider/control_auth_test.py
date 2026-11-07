"""gRPC control authentication fails closed and supports token rotation."""

import grpc
import pytest
import pytest_bazel

from platform_sessions.provider.control_auth import ControlBearerInterceptor


@pytest.mark.asyncio
async def test_authentication_and_secret_rotation(tmp_path):
    token_file = tmp_path / "control-token"
    token_file.write_text("a" * 32, encoding="ascii")
    received = []

    async def handle(request, context):
        received.append(request)
        return b"ok"

    service = "platform_sessions.provider.v1.SessionProviderControl"
    server = grpc.aio.server(interceptors=[ControlBearerInterceptor(token_file)])
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(service, {"Probe": grpc.unary_unary_rpc_method_handler(handle)}),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    assert port
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(f"/{service}/Probe")

            async def denied(metadata):
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await call(b"request", metadata=metadata)
                assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED

            await denied(())
            await denied((("authorization", "Bearer " + "b" * 32),))
            await denied((("authorization", "Bearer " + "a" * 32), ("authorization", "Bearer " + "a" * 32)))
            assert not received
            assert await call(b"one", metadata=(("authorization", "Bearer " + "a" * 32),)) == b"ok"
            token_file.write_text("b" * 32 + "\n", encoding="ascii")
            await denied((("authorization", "Bearer " + "a" * 32),))
            assert await call(b"two", metadata=(("authorization", "Bearer " + "b" * 32),)) == b"ok"
            token_file.unlink()
            await denied((("authorization", "Bearer " + "b" * 32),))
            token_file.write_text("short", encoding="ascii")
            await denied((("authorization", "Bearer short"),))
            assert received == [b"one", b"two"]
    finally:
        await server.stop(grace=0)


if __name__ == "__main__":
    pytest_bazel.main()
