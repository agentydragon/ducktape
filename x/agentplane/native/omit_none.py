"""An outbound frame whose unset options leave no key on the wire at all."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer


class OmitNone(BaseModel):
    """A frame model whose `None` fields are absent from the serialized frame rather than null.

    Both harnesses read an option's key as the option being set: Claude Code's `initialize` schema
    takes `systemPrompt: string | null`, where null is a prompt it must honour, and Codex's
    `thread/start` params are deserialized field by field. An option a caller did not set is
    therefore no key, not a null one, and a frame carrying only the options it means keeps the
    captures of the same interaction byte-identical.
    """

    @model_serializer(mode="wrap")
    def _set_fields_only(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}
