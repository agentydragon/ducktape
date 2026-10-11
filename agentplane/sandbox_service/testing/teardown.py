"""Delete a Sandbox through teardown without a runner, as one whose ingress acknowledged every seal would be."""

from agentplane.sandbox_service.inventory import SandboxInventory


async def seal_and_delete(inventory: SandboxInventory, name: str) -> None:
    uid = (await inventory.get(name)).uid
    await inventory.begin_teardown(name, uid=uid)
    await inventory.delete_flushed(name, uid=uid)
