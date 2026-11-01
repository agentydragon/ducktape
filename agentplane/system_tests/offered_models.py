"""Select models explicitly offered by the deployment for the requested harnesses."""

from collections.abc import Sequence

from agentplane.app.testing.client import Client
from agentplane.env.harnesshost.harness import Harness
from agentplane.proto import runner_pb2


async def models_offered_for(client: Client, harnesses: Sequence[runner_pb2.Harness]) -> dict[runner_pb2.Harness, str]:
    """Select one model offered by the deployment for each requested harness."""
    catalog = await client.models()
    offered_models: dict[runner_pb2.Harness, str] = {}
    for harness in harnesses:
        config_harness = Harness(runner_pb2.Harness.Name(harness))
        offered = catalog.harnesses[config_harness]
        assert offered, f"the deployment offers no model for {harness}"
        offered_models[harness] = offered[0]
    return offered_models


# gazelle:include_dep @pypi//protobuf
