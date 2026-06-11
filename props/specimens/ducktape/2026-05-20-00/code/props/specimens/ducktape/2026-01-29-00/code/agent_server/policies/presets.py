"""Example policy program preset (approve-all)."""

from __future__ import annotations

from agent_server.policies.approve_all import decide
from agent_server.policies.scaffold import run

__all__ = ["decide"]


if __name__ == "__main__":
    raise SystemExit(run(decide))
