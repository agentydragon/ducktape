from __future__ import annotations

import logging
from pathlib import Path

from openai import AsyncOpenAI
from openai.types.responses import ResponseFunctionToolCall, ResponseInputItemParam
from openai.types.responses.easy_input_message import EasyInputMessage
from openai.types.responses.response_input_item import FunctionCallOutput as ResponseFunctionCallOutput
from openai.types.responses.response_input_text import ResponseInputText
from pydantic import BaseModel, TypeAdapter

from .config import OpenAISettings
from .history import ConversationHistory
from .object_store import ObjectStoreClient
from .tool_execution import execute_tool, tool_params
from .tools import build_tool_specs
from .tools.sleep_until_user_message import ConversationStatusProvider

logger = logging.getLogger(__name__)


class OpenAIAgent:
    def __init__(
        self,
        settings: OpenAISettings,
        history: ConversationHistory,
        client: AsyncOpenAI,
        status_provider: ConversationStatusProvider,
        workspace_path: Path,
        object_store: ObjectStoreClient | None,
    ) -> None:
        self._settings = settings
        self._history = history
        self._client = client
        self._wait_for_matrix = False
        self._tool_specs = build_tool_specs(
            self._request_sleep_until_user_message,
            status_provider,
            settings.sleep_tool_policy,
            workspace_path,
            object_store,
        )
        self._tool_params = tool_params(self._tool_specs.values())

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def waiting_for_matrix(self) -> bool:
        return self._wait_for_matrix

    async def handle_user_message(self, content: str) -> None:
        self._wait_for_matrix = False
        self._history.append_input(
            _parse_input_item(
                EasyInputMessage(
                    type="message", role="user", content=[ResponseInputText(type="input_text", text=content)]
                )
            )
        )
        await self._model_loop()

    async def _model_loop(self) -> None:
        iteration = 0
        while True:
            iteration += 1
            input_payload = self._build_input_payload()
            logger.info("Sampling model (iteration %d)", iteration)
            response = await self._client.responses.create(
                model=self.model,
                input=input_payload,
                tools=self._tool_params,
                tool_choice="required",
                include=self._settings.include,
                reasoning=(
                    {"summary": "auto", "effort": self._settings.reasoning_effort}
                    if self._settings.reasoning_effort
                    else {"summary": "auto"}
                ),
            )
            self._history.append_response(response)

            for tool_call in (output for output in response.output if isinstance(output, ResponseFunctionToolCall)):
                await self._execute_tool(tool_call)

            if self._wait_for_matrix:
                logger.info("Model yielded control after %d iterations", iteration)
                break

    async def _execute_tool(self, tool_call: ResponseFunctionToolCall) -> None:
        result = await execute_tool(tool_call, self._tool_specs)
        output = result.model_dump_json() if isinstance(result, BaseModel) else result
        self._history.append_input(
            _parse_input_item(
                ResponseFunctionCallOutput(type="function_call_output", call_id=tool_call.call_id, output=output)
            )
        )

    def _request_sleep_until_user_message(self) -> None:
        self._wait_for_matrix = True

    def _build_input_payload(self) -> list[ResponseInputItemParam]:
        return self._history.build_input_items(self._settings.system_prompt)


def _parse_input_item(model: BaseModel) -> ResponseInputItemParam:
    return _INPUT_ITEM_ADAPTER.validate_python(model.model_dump(mode="python", exclude_none=True))


_INPUT_ITEM_ADAPTER: TypeAdapter[ResponseInputItemParam] = TypeAdapter(ResponseInputItemParam)
