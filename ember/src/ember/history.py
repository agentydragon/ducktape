from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import logging
from pathlib import Path

from openai.types.responses import Response, ResponseInputItemParam, ResponseInputParam
from openai.types.responses.easy_input_message import EasyInputMessage
from openai.types.responses.response_input_text import ResponseInputText
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

_INPUT_ITEM_ADAPTER: TypeAdapter[ResponseInputItemParam] = TypeAdapter(ResponseInputItemParam)
_INPUT_ADAPTER: TypeAdapter[ResponseInputParam] = TypeAdapter(ResponseInputParam)


class HistoryRecord(BaseModel):
    timestamp: datetime
    input_item: ResponseInputItemParam | None = None
    response: Response | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def for_input(cls, item: ResponseInputItemParam) -> HistoryRecord:
        return cls(timestamp=_utcnow(), input_item=item)

    @classmethod
    def for_response(cls, response: Response) -> HistoryRecord:
        return cls(timestamp=_utcnow(), response=response)


class ConversationHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[HistoryRecord] = list(self._load_existing(path))

    def append_input(self, item: ResponseInputItemParam) -> None:
        self._records.append(HistoryRecord.for_input(item))
        self._persist()

    def append_response(self, response: Response) -> None:
        self._records.append(HistoryRecord.for_response(response))
        self._persist()

    def build_input_items(self, system_prompt: str) -> ResponseInputParam:
        items: list[ResponseInputItemParam] = []
        system_message = EasyInputMessage(
            type="message", role="system", content=[ResponseInputText(type="input_text", text=system_prompt)]
        )
        items.append(_INPUT_ITEM_ADAPTER.validate_python(system_message.model_dump(mode="python", exclude_none=True)))

        for record in self._records:
            if (input_item := record.input_item) is not None:
                items.append(input_item)
                continue
            if (response := record.response) is None:
                continue
            for output in response.output:
                try:
                    items.append(
                        _INPUT_ITEM_ADAPTER.validate_python(output.model_dump(mode="python", exclude_none=True))
                    )
                except ValidationError as exc:
                    logger.warning("Skipping response output due to validation error: %s", exc)
                    continue

        # TODO: handle out-of-context errors by compacting history via OpenAI summarisation.
        return _INPUT_ADAPTER.validate_python(items)

    def _persist(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(record.model_dump_json())
                handle.write("\n")
        tmp_path.replace(self.path)

    def _load_existing(self, path: Path) -> Iterable[HistoryRecord]:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    yield HistoryRecord.model_validate_json(line)
                except ValidationError as exc:
                    logger.debug("Skipping invalid history record: %s", exc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
