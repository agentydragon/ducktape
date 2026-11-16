"""Test helpers for driving agent steps until the model produces text."""

from skills.info_gathering.evals.agent_runtime.agent import Agent
from skills.info_gathering.evals.agent_runtime.events import AssistantText
from skills.info_gathering.evals.agent_runtime.handler import BaseHandler


class TextCaptureHandler(BaseHandler):
    def __init__(self) -> None:
        self.text: list[str] = []

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self.text.append(evt.text)


async def run_until_text(agent: Agent, capture: TextCaptureHandler) -> str:
    """Step until text is emitted; bound test work if the model never responds."""
    for _ in range(20):
        await agent.step()
        if capture.text:
            return "\n".join(capture.text)
    raise AssertionError("Agent did not produce text within 20 steps")
