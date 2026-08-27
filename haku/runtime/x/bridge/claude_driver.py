"""Claude Code's native-protocol driving, runner-side, for the neutral-operation generation.

What the Console's Claude client used to do beside the fold moves here with the fold (#4667):
composing the native input a dispatched prompt becomes, the `initialize` handshake, the operator's
interrupt, and answering the CLI's own inbound control requests — an unanswered one blocks the CLI
forever (measured: <../../../cli_protocol/probes/harness.py>). The projection itself is
<claude_projection.py>; this driver owns everything that must be *written* natively.

Control responses are deliberately not awaited. The v3 client correlated them to report errors to
its caller; this end has no caller to tell — the response comes back on stdout, is numbered and
recorded like every frame, and an operator reads it in the rollout. Ordering on stdin is what the
initialize handshake actually needs: the CLI reads its input sequentially, so a prompt written
after the initialize request is read after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from haku.cli_protocol.frames import ControlRequestFrame, ControlResponse, ControlSubtype, InterruptRequest
from haku.runtime.x.bridge.claude_projection import ClaudeProjector, Projected


@dataclass(slots=True)
class ClaudeDriver:
    """One CLI process's native-protocol companion: what to write, and what its stream means."""

    projector: ClaudeProjector = field(default_factory=ClaudeProjector)

    def initialize(self) -> dict[str, Any] | None:
        """The handshake frame to write before anything else, or None for a harness without one.

        Bare: the launch argv already carries the system prompt and MCP wiring
        (<claude_options.py>), so the request only has to exist for the CLI to start serving.
        """
        return ControlRequestFrame(request={"subtype": "initialize"}).model_dump()

    def compose_prompt(self, text: str) -> dict[str, Any]:
        """One dispatched prompt as the CLI's native input frame.

        The `uuid` turns `command_lifecycle` reporting on and makes the prompt reachable by
        `interrupt`'s `cancel_queued`, exactly as the v3 client sent it.
        """
        return {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "uuid": str(uuid4()),
        }

    def compose_interrupt(self) -> dict[str, Any] | None:
        """Abort the running turn and anything queued behind it.

        `cancel_queued` is not optional: a bare interrupt makes the CLI begin the next queued
        prompt the moment this one dies, and an operator saying "stop" does not mean "start the
        next thing I typed".
        """
        return ControlRequestFrame(
            request=InterruptRequest(reason="user-cancel", cancel_queued=True).model_dump(exclude_none=True)
        ).model_dump()

    def answer_control_request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The refusal to write back for a CLI → runner control request, or None for other frames.

        This session registers no hooks, no `can_use_tool` and no SDK-hosted MCP server, so there
        is nothing the CLI should be asking — but it must still be answered, or it waits forever.
        """
        if payload.get("type") != "control_request":
            return None
        subtype = (payload.get("request") or {}).get("subtype")
        refusal = ControlResponse(
            subtype=ControlSubtype.ERROR,
            request_id=payload["request_id"],
            error=f"{subtype} is not supported by this runner",
        )
        return {"type": "control_response", "response": refusal.model_dump(exclude_none=True)}

    def observe(self, frame_seq: int, payload: dict[str, Any]) -> Projected:
        return self.projector.observe(frame_seq, payload)

    def admit(self, prompt_id: UUID, *, after_batch_seq: int | None, frame_seq: int | None) -> Projected:
        return self.projector.admit(prompt_id, after_batch_seq=after_batch_seq, frame_seq=frame_seq)
