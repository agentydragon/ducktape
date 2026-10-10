"""Delete a Sandbox through teardown without a runner, as one whose runner sealed no Session would."""

from collections.abc import Mapping

from agentplane.sandbox_service.inventory import SandboxInventory


async def seal_and_delete(inventory: SandboxInventory, name: str, *, cursors: Mapping[str, int] | None = None) -> None:
    uid = (await inventory.get(name)).uid
    await inventory.begin_teardown(name, uid=uid)
    await inventory.record_seal(name, uid=uid, cursors=cursors or {})
    assert await inventory.delete_if_released(name, uid=uid)
