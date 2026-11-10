"""Scripted model configuration service for Environment Runtime integration fixtures."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import override

import grpc

from agentplane.grpc.serving import serving
from agentplane.llm_ingress.v1 import model_config_pb2, model_config_pb2_grpc

# gazelle:include_dep @pypi//protobuf


class _ModelConfig(model_config_pb2_grpc.ModelConfigServiceServicer):
    @override
    async def GetModelConfig(
        self, request: model_config_pb2.GetModelConfigRequest, context: grpc.aio.ServicerContext
    ) -> model_config_pb2.GetModelConfigResponse:
        return model_config_pb2.GetModelConfigResponse()


@asynccontextmanager
async def model_config() -> AsyncIterator[str]:
    server = grpc.aio.server()
    model_config_pb2_grpc.add_ModelConfigServiceServicer_to_server(_ModelConfig(), server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield f"127.0.0.1:{port}"
