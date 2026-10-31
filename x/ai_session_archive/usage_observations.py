"""Extract source usage counters from a single decoded JSONL record."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class UsageCounterKind(StrEnum):
    RESPONSE = "response"
    LAST_RESPONSE = "last_response"
    TURN = "turn"
    THREAD = "thread"
    SESSION = "session"
    MESSAGE = "message"
    RESULT = "result"
    MODEL_RESULT = "model_result"


@dataclass(frozen=True)
class UsageObservation:
    source_field: str
    counter_kind: UsageCounterKind
    model: str | None
    response_id: str | None
    message_id: str | None
    turn_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


_COUNTER_KEYS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_read_input_tokens": ("cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens"),
    "cache_write_input_tokens": ("cache_write_input_tokens", "cache_creation_input_tokens", "cacheCreationInputTokens"),
    "reasoning_output_tokens": ("reasoning_output_tokens", "thinkingTokens"),
    "total_tokens": ("total_tokens",),
}


def usage_observations_for_record(record: Any) -> list[UsageObservation]:
    """Return only usage explicitly present in this record, preserving each source block."""
    if not isinstance(record, Mapping):
        return []

    kind = record.get("type")
    if kind == "token_usage_record":
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return []
        return [
            _observation(
                payload[field],
                source_field=f"payload.{field}",
                counter_kind=counter_kind,
                model=_string(payload.get("model")),
                response_id=_string(payload.get("response_id")),
                message_id=None,
                turn_id=_string(payload.get("turn_id")),
            )
            for field, counter_kind in (
                ("usage", UsageCounterKind.RESPONSE),
                ("turn_token_usage", UsageCounterKind.TURN),
                ("thread_token_usage", UsageCounterKind.THREAD),
            )
            if isinstance(payload.get(field), Mapping)
        ]

    if kind == "event_msg":
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
            return []
        info = payload.get("info")
        if not isinstance(info, Mapping):
            return []
        return [
            _observation(
                info[field],
                source_field=f"payload.info.{field}",
                counter_kind=counter_kind,
                model=None,
                response_id=_string(payload.get("response_id")),
                message_id=None,
                turn_id=_string(payload.get("turn_id")),
            )
            for field, counter_kind in (
                ("last_token_usage", UsageCounterKind.LAST_RESPONSE),
                ("total_token_usage", UsageCounterKind.SESSION),
            )
            if isinstance(info.get(field), Mapping)
        ]

    if kind == "assistant":
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if not isinstance(message, Mapping) or not isinstance(usage, Mapping):
            return []
        return [
            _observation(
                usage,
                source_field="message.usage",
                counter_kind=UsageCounterKind.MESSAGE,
                model=_string(message.get("model")),
                response_id=None,
                message_id=_string(message.get("id")),
                turn_id=None,
            )
        ]

    if kind == "result":
        observations = []
        usage = record.get("usage")
        if isinstance(usage, Mapping):
            observations.append(
                _observation(
                    usage,
                    source_field="usage",
                    counter_kind=UsageCounterKind.RESULT,
                    model=_string(record.get("model")),
                    response_id=None,
                    message_id=None,
                    turn_id=None,
                )
            )
        model_usage = record.get("modelUsage")
        if isinstance(model_usage, Mapping):
            for model, model_counters in model_usage.items():
                if isinstance(model, str) and isinstance(model_counters, Mapping):
                    observations.append(
                        _observation(
                            model_counters,
                            source_field=f"modelUsage[{json.dumps(model)}]",
                            counter_kind=UsageCounterKind.MODEL_RESULT,
                            model=model,
                            response_id=None,
                            message_id=None,
                            turn_id=None,
                        )
                    )
        return observations

    return []


def _observation(
    counters: Mapping[str, Any],
    *,
    source_field: str,
    counter_kind: UsageCounterKind,
    model: str | None,
    response_id: str | None,
    message_id: str | None,
    turn_id: str | None,
) -> UsageObservation:
    reasoning_output_tokens = _counter(counters, _COUNTER_KEYS["reasoning_output_tokens"])
    if reasoning_output_tokens is None:
        output_details = counters.get("output_tokens_details")
        if isinstance(output_details, Mapping):
            reasoning_output_tokens = _counter(output_details, ("thinking_tokens",))
    return UsageObservation(
        source_field=source_field,
        counter_kind=counter_kind,
        model=model,
        response_id=response_id,
        message_id=message_id,
        turn_id=turn_id,
        input_tokens=_counter(counters, _COUNTER_KEYS["input_tokens"]),
        output_tokens=_counter(counters, _COUNTER_KEYS["output_tokens"]),
        cache_read_input_tokens=_counter(counters, _COUNTER_KEYS["cache_read_input_tokens"]),
        cache_write_input_tokens=_counter(counters, _COUNTER_KEYS["cache_write_input_tokens"]),
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=_counter(counters, _COUNTER_KEYS["total_tokens"]),
    )


def _counter(counters: Mapping[str, Any], source_keys: tuple[str, ...]) -> int | None:
    for key in source_keys:
        value = counters.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
