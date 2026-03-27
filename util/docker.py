"""Docker network utilities."""

from __future__ import annotations

import aiodocker


async def get_docker_network_gateway_async(docker_client: aiodocker.Docker, network_name: str) -> str:
    """Get gateway IP for Docker network (async version).

    Args:
        docker_client: Async Docker client
        network_name: Name of the Docker network

    Returns:
        Gateway IP address (e.g., "172.19.0.1")

    Raises:
        RuntimeError: If network does not exist or gateway cannot be determined
    """
    networks = await docker_client.networks.list()
    network = next((n for n in networks if n["Name"] == network_name), None)
    if not network:
        raise RuntimeError(f"Network not found: {network_name}")

    network_obj = await docker_client.networks.get(network["Id"])
    network_info = await network_obj.show()
    ipam_config = network_info.get("IPAM", {}).get("Config", [])
    if not ipam_config:
        raise RuntimeError(f"No IPAM config for network {network_name}")

    gateway = ipam_config[0].get("Gateway")
    if isinstance(gateway, str):
        return gateway
    raise RuntimeError(f"No gateway found for network {network_name}")
