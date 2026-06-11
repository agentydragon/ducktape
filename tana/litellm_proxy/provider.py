from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import litellm
from litellm import CustomLLM
from litellm.types.llms.openai import ChatCompletionToolCallChunk
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage
from litellm.utils import custom_llm_setup

DEFAULT_FIREBASE_API_KEY = "AIzaSyA9LtJM6Ga9VAwCfj9w_mNORdOaq2yLshQ"
DEFAULT_FUNCTIONS_BASE_URL = "https://app.tana.inc/functions"
DEFAULT_REFRESH_TOKEN_SECRET = "tana-mcp/tana-firebase-refresh-token"
DEFAULT_REFRESH_TOKEN_KEY = "refresh_token"
TANA_PROVIDER = "tana"


class TanaProxyError(RuntimeError):
    """Raised when a Tana proxy request or token lookup fails."""


@dataclass(frozen=True)
class TanaProxyConfig:
    firebase_api_key: str = DEFAULT_FIREBASE_API_KEY
    functions_base_url: str = DEFAULT_FUNCTIONS_BASE_URL
    user_context: str = "Generic AI Query"
    tool_user_context: str = "Ask Tana"
    refresh_token: str | None = None
    refresh_token_file: str | None = None
    refresh_token_secret: str = DEFAULT_REFRESH_TOKEN_SECRET
    refresh_token_secret_key: str = DEFAULT_REFRESH_TOKEN_KEY
    request_timeout_seconds: float = 60.0
    ignore_large_context_warning: bool = True
    ignore_out_of_credits_warning: bool = False

    @classmethod
    def from_env(cls) -> TanaProxyConfig:
        return cls(
            firebase_api_key=os.environ.get("TANA_FIREBASE_API_KEY", DEFAULT_FIREBASE_API_KEY),
            functions_base_url=os.environ.get("TANA_FUNCTIONS_BASE_URL", DEFAULT_FUNCTIONS_BASE_URL),
            user_context=os.environ.get("TANA_LLM_USER_CONTEXT", "Generic AI Query"),
            tool_user_context=os.environ.get("TANA_LLM_TOOL_USER_CONTEXT", "Ask Tana"),
            refresh_token=os.environ.get("TANA_FIREBASE_REFRESH_TOKEN"),
            refresh_token_file=os.environ.get("TANA_FIREBASE_REFRESH_TOKEN_FILE"),
            refresh_token_secret=os.environ.get("TANA_FIREBASE_REFRESH_TOKEN_SECRET", DEFAULT_REFRESH_TOKEN_SECRET),
            refresh_token_secret_key=os.environ.get("TANA_FIREBASE_REFRESH_TOKEN_KEY", DEFAULT_REFRESH_TOKEN_KEY),
            request_timeout_seconds=float(os.environ.get("TANA_LLM_TIMEOUT_SECONDS", "60")),
            ignore_large_context_warning=_env_bool("TANA_IGNORE_LARGE_CONTEXT_WARNING", True),
            ignore_out_of_credits_warning=_env_bool("TANA_IGNORE_OUT_OF_CREDITS_WARNING", False),
        )


@dataclass(frozen=True)
class FreshTokens:
    id_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class TanaChatResult:
    text: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_results: list[Any] | None = None
    usage: dict[str, int] | None = None
    raw: Any | None = None


class _ChatClient(Protocol):
    async def chat_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> TanaChatResult: ...

    def stream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> Iterator[GenericStreamingChunk]: ...

    def astream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[GenericStreamingChunk]: ...


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _secret_ref_parts(secret_ref: str) -> tuple[str, str]:
    if "/" not in secret_ref:
        raise TanaProxyError(f"TANA_FIREBASE_REFRESH_TOKEN_SECRET must be namespace/name, got {secret_ref!r}")
    namespace, name = secret_ref.split("/", 1)
    if not namespace or not name:
        raise TanaProxyError(f"TANA_FIREBASE_REFRESH_TOKEN_SECRET must be namespace/name, got {secret_ref!r}")
    return namespace, name


def read_refresh_token_from_config(cfg: TanaProxyConfig, runner: Runner = _run_command) -> str:
    if cfg.refresh_token:
        return cfg.refresh_token.strip()
    if cfg.refresh_token_file:
        with Path(cfg.refresh_token_file).open(encoding="utf-8") as token_file:
            return token_file.read().strip()

    namespace, name = _secret_ref_parts(cfg.refresh_token_secret)
    try:
        completed = runner(["kubectl", "get", "secret", "-n", namespace, name, "-o", "json"])
    except FileNotFoundError as exc:
        raise TanaProxyError(
            "kubectl is required when TANA_FIREBASE_REFRESH_TOKEN or TANA_FIREBASE_REFRESH_TOKEN_FILE is not set"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise TanaProxyError(f"failed to read Kubernetes secret {namespace}/{name}: {stderr}") from exc

    try:
        secret = json.loads(completed.stdout)
        encoded = secret["data"][cfg.refresh_token_secret_key]
        return base64.b64decode(encoded).decode("utf-8").strip()
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise TanaProxyError(
            f"Kubernetes secret {namespace}/{name} is missing data key {cfg.refresh_token_secret_key!r}"
        ) from exc


async def _refresh_id_token_once(http: httpx.AsyncClient, firebase_api_key: str, refresh_token: str) -> FreshTokens:
    response = await http.post(
        "https://securetoken.googleapis.com/v1/token",
        params={"key": firebase_api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TanaProxyError(
            f"Firebase refresh-token exchange failed with HTTP {response.status_code}: {_body_snippet(response)}"
        ) from exc
    data = response.json()
    return FreshTokens(
        id_token=data["id_token"], refresh_token=data["refresh_token"], expires_in=int(data["expires_in"])
    )


def _refresh_id_token_once_sync(http: httpx.Client, firebase_api_key: str, refresh_token: str) -> FreshTokens:
    response = http.post(
        "https://securetoken.googleapis.com/v1/token",
        params={"key": firebase_api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TanaProxyError(
            f"Firebase refresh-token exchange failed with HTTP {response.status_code}: {_body_snippet(response)}"
        ) from exc
    data = response.json()
    return FreshTokens(
        id_token=data["id_token"], refresh_token=data["refresh_token"], expires_in=int(data["expires_in"])
    )


class TanaProxyClient:
    def __init__(
        self,
        cfg: TanaProxyConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        sync_http_client: httpx.Client | None = None,
        refresh_token_reader: Callable[[TanaProxyConfig], str] = read_refresh_token_from_config,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg or TanaProxyConfig.from_env()
        self._http_client = http_client
        self._sync_http_client = sync_http_client
        self._refresh_token_reader = refresh_token_reader
        self._now = now
        self._id_token: str | None = None
        self._id_token_expires_at = 0.0
        self._refresh_token: str | None = self._cfg.refresh_token

    async def chat_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> TanaChatResult:
        if self._http_client is not None:
            return await self._chat_completion(self._http_client, model, messages, optional_params or {})

        async with httpx.AsyncClient(timeout=self._cfg.request_timeout_seconds) as http:
            return await self._chat_completion(http, model, messages, optional_params or {})

    async def _chat_completion(
        self,
        http: httpx.AsyncClient,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any],
    ) -> TanaChatResult:
        _reject_unsupported(optional_params)
        id_token = await self._id_token_for_request(http)
        if _has_tools(optional_params):
            return await self._tool_chat_completion(http, id_token, model, messages, optional_params)
        args = _basic_chat_args(self._cfg.user_context, _strip_tana_prefix(model), messages, optional_params, self._cfg)
        body = {"isStreaming": False, "args": args}
        response = await http.post(
            f"{self._cfg.functions_base_url.rstrip('/')}/llmProxy",
            json=body,
            headers={
                "Authorization": f"Bearer {id_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise TanaProxyError(f"Tana llmProxy failed with HTTP {response.status_code}: {_body_snippet(response)}")
        return _parse_tana_response(response)

    async def _tool_chat_completion(
        self,
        http: httpx.AsyncClient,
        id_token: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any],
    ) -> TanaChatResult:
        body = {
            "isStreaming": False,
            "args": {
                "userContext": self._cfg.tool_user_context,
                "messages": _normalize_messages(messages),
                "options": _tana_options(_strip_tana_prefix(model), optional_params, self._cfg),
            },
            "dynamicTools": _dynamic_tools(optional_params),
        }
        response = await http.post(
            f"{self._cfg.functions_base_url.rstrip('/')}/llmProxyNext",
            json=body,
            headers={
                "Authorization": f"Bearer {id_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise TanaProxyError(
                f"Tana llmProxyNext failed with HTTP {response.status_code}: {_body_snippet(response)}"
            )
        return _parse_tana_response(response)

    async def _id_token_for_request(self, http: httpx.AsyncClient) -> str:
        if self._id_token is not None and self._now() < self._id_token_expires_at - 60:
            return self._id_token

        refresh_token = self._refresh_token or self._refresh_token_reader(self._cfg)
        fresh = await _refresh_id_token_once(http, self._cfg.firebase_api_key, refresh_token)
        self._id_token = fresh.id_token
        self._id_token_expires_at = self._now() + fresh.expires_in
        return fresh.id_token

    def stream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> Iterator[GenericStreamingChunk]:
        if self._sync_http_client is not None:
            yield from self._stream_completion(self._sync_http_client, model, messages, optional_params or {})
            return

        with httpx.Client(timeout=self._cfg.request_timeout_seconds) as http:
            yield from self._stream_completion(http, model, messages, optional_params or {})

    def _stream_completion(
        self, http: httpx.Client, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any]
    ) -> Iterator[GenericStreamingChunk]:
        _reject_unsupported(optional_params)
        id_token = self._id_token_for_request_sync(http)
        url, body = self._stream_request(_strip_tana_prefix(model), messages, optional_params)
        headers = {
            "Authorization": f"Bearer {id_token}",
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
        }
        with http.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                response.read()
                endpoint = "llmProxyNext" if _has_tools(optional_params) else "llmProxy"
                raise TanaProxyError(
                    f"Tana {endpoint} streaming failed with HTTP {response.status_code}: {_body_snippet(response)}"
                )
            yield from _parse_tana_stream_lines(response.iter_lines())

    async def astream_completion(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[GenericStreamingChunk]:
        if self._http_client is not None:
            async for chunk in self._astream_completion(self._http_client, model, messages, optional_params or {}):
                yield chunk
            return

        async with httpx.AsyncClient(timeout=self._cfg.request_timeout_seconds) as http:
            async for chunk in self._astream_completion(http, model, messages, optional_params or {}):
                yield chunk

    async def _astream_completion(
        self,
        http: httpx.AsyncClient,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        optional_params: Mapping[str, Any],
    ) -> AsyncIterator[GenericStreamingChunk]:
        _reject_unsupported(optional_params)
        id_token = await self._id_token_for_request(http)
        url, body = self._stream_request(_strip_tana_prefix(model), messages, optional_params)
        headers = {
            "Authorization": f"Bearer {id_token}",
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
        }
        async with http.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                endpoint = "llmProxyNext" if _has_tools(optional_params) else "llmProxy"
                raise TanaProxyError(
                    f"Tana {endpoint} streaming failed with HTTP {response.status_code}: {_body_snippet(response)}"
                )
            async for chunk in _parse_tana_stream_lines_async(response.aiter_lines()):
                yield chunk

    def _stream_request(
        self, model: str, messages: Sequence[Mapping[str, Any]], optional_params: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if _has_tools(optional_params):
            return (
                f"{self._cfg.functions_base_url.rstrip('/')}/llmProxyNext",
                {
                    "isStreaming": True,
                    "args": {
                        "userContext": self._cfg.tool_user_context,
                        "messages": _normalize_messages(messages),
                        "options": _tana_options(model, optional_params, self._cfg),
                    },
                    "dynamicTools": _dynamic_tools(optional_params),
                },
            )
        return (
            f"{self._cfg.functions_base_url.rstrip('/')}/llmProxy",
            {
                "isStreaming": True,
                "args": _basic_chat_args(self._cfg.user_context, model, messages, optional_params, self._cfg),
            },
        )

    def _id_token_for_request_sync(self, http: httpx.Client) -> str:
        if self._id_token is not None and self._now() < self._id_token_expires_at - 60:
            return self._id_token

        refresh_token = self._refresh_token or self._refresh_token_reader(self._cfg)
        fresh = _refresh_id_token_once_sync(http, self._cfg.firebase_api_key, refresh_token)
        self._id_token = fresh.id_token
        self._id_token_expires_at = self._now() + fresh.expires_in
        return fresh.id_token


class TanaLiteLLM(CustomLLM):
    def __init__(self, client: _ChatClient | None = None) -> None:
        super().__init__()
        self._client = client or TanaProxyClient()

    def completion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acompletion(*args, **kwargs))
        raise TanaProxyError("Use litellm.acompletion() when calling TanaLiteLLM from an active event loop")

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = _required_kwarg("model", kwargs)
        messages = _required_kwarg("messages", kwargs)
        optional_params = kwargs.get("optional_params") or {}
        result = await self._client.chat_completion(model, messages, optional_params)
        return _model_response(model, result)

    def streaming(self, *args: Any, **kwargs: Any) -> Iterator[GenericStreamingChunk]:
        model = _required_kwarg("model", kwargs)
        messages = _required_kwarg("messages", kwargs)
        optional_params = kwargs.get("optional_params") or {}
        yield from _filter_stream_chunks(self._client.stream_completion(model, messages, optional_params))

    # LiteLLM's base type annotates this as a coroutine, but the streaming
    # dispatcher consumes the returned object as an async iterator.
    async def astreaming(  # type: ignore[override]
        self,
        model: str,
        messages: list[Any],
        api_base: str,
        custom_prompt_dict: dict[Any, Any],
        model_response: ModelResponse,
        print_verbose: Callable[..., Any],
        encoding: Any,
        api_key: Any,
        logging_obj: Any,
        optional_params: dict[Any, Any],
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: Any = None,
        timeout: Any = None,  # noqa: ASYNC109 - LiteLLM's override signature includes timeout.
        client: Any = None,
    ) -> AsyncIterator[GenericStreamingChunk]:
        del api_base, custom_prompt_dict, model_response, print_verbose, encoding, api_key
        del logging_obj, acompletion, litellm_params, logger_fn, headers, timeout, client
        async for chunk in self._client.astream_completion(
            model, cast(Sequence[Mapping[str, Any]], messages), optional_params
        ):
            if _is_empty_nonterminal_stream_chunk(chunk):
                continue
            yield chunk
            if _is_terminal_stream_chunk(chunk):
                break


def register_litellm_provider(handler: TanaLiteLLM | None = None) -> TanaLiteLLM:
    custom_handler = handler or TanaLiteLLM()
    litellm.custom_provider_map = [
        item for item in litellm.custom_provider_map if item.get("provider") != TANA_PROVIDER
    ]
    litellm.custom_provider_map.append({"provider": TANA_PROVIDER, "custom_handler": custom_handler})
    custom_llm_setup()
    ensure_tana_custom_provider_dispatch()
    return custom_handler


def ensure_tana_custom_provider_dispatch() -> None:
    if TANA_PROVIDER not in litellm.provider_list:
        litellm.provider_list.append(TANA_PROVIDER)
    if TANA_PROVIDER not in litellm._custom_providers:
        litellm._custom_providers.append(TANA_PROVIDER)
    litellm.model_list_set.add(TANA_PROVIDER)


def _required_kwarg(name: str, kwargs: Mapping[str, Any]) -> Any:
    if name not in kwargs:
        raise TanaProxyError(f"LiteLLM did not pass required argument {name!r}")
    return kwargs[name]


def _strip_tana_prefix(model: str) -> str:
    if model.startswith("tana/"):
        return model.removeprefix("tana/")
    return model


def _display_model(model: str) -> str:
    if model.startswith("tana/"):
        return model
    return f"tana/{model}"


def _reject_unsupported(optional_params: Mapping[str, Any]) -> None:
    for key in ("function_call",):
        if optional_params.get(key):
            raise NotImplementedError(f"Tana LiteLLM provider does not support {key!r} yet")


def _has_tools(optional_params: Mapping[str, Any]) -> bool:
    if optional_params.get("tool_choice") == "none":
        return False
    tools = optional_params.get("tools")
    functions = optional_params.get("functions")
    return bool(tools or functions)


def _basic_chat_args(
    user_context: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    optional_params: Mapping[str, Any],
    cfg: TanaProxyConfig,
) -> dict[str, Any]:
    return {
        "userContext": user_context,
        "messages": _normalize_messages(messages),
        "options": _tana_options(model, optional_params, cfg),
    }


def _dynamic_tools(optional_params: Mapping[str, Any]) -> list[dict[str, Any]]:
    dynamic_tools: list[dict[str, Any]] = []
    for tool in optional_params.get("tools") or []:
        if not isinstance(tool, Mapping):
            raise TanaProxyError(f"expected tool mapping, got {type(tool).__name__}")
        if tool.get("type") != "function":
            raise NotImplementedError("Tana LiteLLM provider only supports OpenAI function tools")
        function = tool.get("function")
        if not isinstance(function, Mapping):
            raise TanaProxyError(f"tool is missing function definition: {tool!r}")
        dynamic_tools.append(_dynamic_tool_from_function(function))
    for function in optional_params.get("functions") or []:
        if not isinstance(function, Mapping):
            raise TanaProxyError(f"expected function mapping, got {type(function).__name__}")
        dynamic_tools.append(_dynamic_tool_from_function(function))
    if not dynamic_tools:
        raise TanaProxyError("tool request did not include any function tools")
    return dynamic_tools


def _dynamic_tool_from_function(function: Mapping[str, Any]) -> dict[str, Any]:
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise TanaProxyError(f"function tool is missing a non-empty name: {function!r}")
    parameters = function.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, Mapping):
        raise TanaProxyError(f"function tool {name!r} parameters must be a JSON object")
    description = function.get("description")
    return {
        "name": name,
        "description": str(description) if description else f"Execute {name}",
        "kind": "mcpTool",
        "runtime": "client",
        "schema": parameters,
    }


def _normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            raise TanaProxyError(f"expected LiteLLM message mappings, got {type(message).__name__}")
        role = message.get("role")
        if role is None:
            raise TanaProxyError(f"message is missing required role: {message!r}")
        content = _normalize_content(message.get("content", ""))
        system_provider_options: dict[str, Any] | None = None
        if role == "system":
            content, system_provider_options = _normalize_system_content(content)
        if role == "user" and _content_has_tool_result(content):
            role = "tool"
            content = _tool_result_parts(content)
        tool_calls = message.get("tool_calls")
        if tool_calls is None:
            tool_calls = message.get("toolCalls")
        if tool_calls is not None:
            content = _append_content_parts(content, _normalize_tool_call_parts(tool_calls))
        if role == "tool":
            content = _normalize_tool_result_content(message, content, tool_names_by_id)
        normalized_message = {"role": role, "content": content}
        if system_provider_options is not None:
            _merge_provider_options(normalized_message, system_provider_options)
        _merge_first_set_provider_options(message, normalized_message, ("providerOptions", "provider_options"))
        _copy_first_set(message, normalized_message, ("cache_control", "cacheControl"), "cache_control")
        _move_anthropic_cache_control(normalized_message)
        normalized.append(normalized_message)
        _remember_tool_call_names(content, tool_names_by_id)
    return normalized


def _normalize_system_content(content: Any) -> tuple[str, dict[str, Any] | None]:
    if isinstance(content, str):
        return content, None
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        return str(content or ""), None

    text_blocks: list[str] = []
    provider_options: dict[str, Any] = {}
    for part in content:
        if isinstance(part, str):
            text_blocks.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        part_provider_options = part.get("providerOptions")
        if isinstance(part_provider_options, Mapping):
            _merge_provider_options_value(provider_options, part_provider_options)
        part_text = part.get("text")
        if isinstance(part_text, str):
            text_blocks.append(part_text)
    return "\n".join(text_blocks), provider_options or None


def _normalize_content(content: Any) -> Any:
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        return [_normalize_content_part(part) for part in content]
    return content


def _normalize_content_part(part: Any) -> Any:
    if not isinstance(part, Mapping):
        return part
    normalized = dict(part)
    if normalized.get("type") == "input_text":
        normalized["type"] = "text"
    if normalized.get("type") == "tool_use":
        normalized["type"] = "tool-call"
        _move_alias(normalized, "id", "toolCallId")
        _move_alias(normalized, "name", "toolName")
    if normalized.get("type") == "tool_result":
        normalized["type"] = "tool-result"
        _move_alias(normalized, "tool_use_id", "toolCallId")
    _move_alias(normalized, "provider_options", "providerOptions")
    _move_alias(normalized, "tool_call_id", "toolCallId")
    _move_alias(normalized, "tool_name", "toolName")
    _move_anthropic_cache_control(normalized)
    if normalized.get("type") == "tool-call":
        arguments = normalized.pop("arguments", None)
        if arguments is not None and normalized.get("input") is None:
            normalized["input"] = _parse_tool_arguments(arguments)
    if normalized.get("type") == "tool-result":
        result_content = normalized.pop("content", None)
        if result_content is not None and normalized.get("output") is None:
            normalized["output"] = _normalize_tool_result_output(result_content, bool(normalized.get("is_error")))
        normalized.pop("is_error", None)
    return normalized


def _normalize_tool_call_parts(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (bytes, bytearray, str)):
        raise TanaProxyError(f"expected tool_calls sequence, got {type(tool_calls).__name__}")
    parts: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            raise TanaProxyError(f"expected tool_call mapping, got {type(tool_call).__name__}")
        normalized = dict(tool_call)
        _move_alias(normalized, "tool_call_id", "toolCallId")
        _move_alias(normalized, "tool_name", "toolName")
        function = normalized.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            arguments = function.get("arguments")
            if name is not None and normalized.get("toolName") is None:
                normalized["toolName"] = name
            if arguments is not None and normalized.get("input") is None:
                normalized["input"] = _parse_tool_arguments(arguments)
            normalized.pop("function", None)
        if normalized.get("id") is not None and normalized.get("toolCallId") is None:
            normalized["toolCallId"] = normalized["id"]
        if normalized.get("name") is not None and normalized.get("toolName") is None:
            normalized["toolName"] = normalized["name"]
        tool_call_id = normalized.get("toolCallId")
        tool_name = normalized.get("toolName")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise TanaProxyError(f"assistant tool call is missing toolCallId/id: {tool_call!r}")
        if not isinstance(tool_name, str) or not tool_name:
            raise TanaProxyError(f"assistant tool call is missing toolName/function.name: {tool_call!r}")
        part = {
            "type": "tool-call",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "input": normalized.get("input", normalized.get("args", {})),
        }
        _copy_if_set(normalized, part, "providerOptions", "providerOptions")
        _copy_if_set(normalized, part, "providerExecuted", "providerExecuted")
        _copy_if_set(normalized, part, "thoughtSignature", "thoughtSignature")
        _copy_if_set(normalized, part, "thought", "thought")
        parts.append(part)
    return parts


def _append_content_parts(content: Any, parts: Sequence[dict[str, Any]]) -> Any:
    if not parts:
        return content
    if content in (None, ""):
        return list(parts)
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        return [*content, *parts]
    if isinstance(content, str):
        return [{"type": "text", "text": content}, *parts]
    return [content, *parts]


def _content_has_tool_result(content: Any) -> bool:
    return (
        isinstance(content, Sequence)
        and not isinstance(content, (bytes, bytearray, str))
        and any(isinstance(part, Mapping) and part.get("type") == "tool-result" for part in content)
    )


def _tool_result_parts(content: Any) -> list[Any]:
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray, str)):
        return [content]
    return [part for part in content if isinstance(part, Mapping) and part.get("type") == "tool-result"]


def _normalize_tool_result_content(
    message: Mapping[str, Any], content: Any, tool_names_by_id: Mapping[str, str]
) -> Any:
    tool_call_id = message.get("toolCallId") or message.get("tool_call_id")
    tool_name = message.get("toolName") or message.get("name")
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        normalized_parts = []
        has_tool_result = False
        for part in content:
            normalized_part = _normalize_content_part(part)
            if isinstance(normalized_part, Mapping) and normalized_part.get("type") == "tool-result":
                has_tool_result = True
                normalized_part = dict(normalized_part)
                if tool_call_id is not None and normalized_part.get("toolCallId") is None:
                    normalized_part["toolCallId"] = tool_call_id
                part_tool_call_id = normalized_part.get("toolCallId")
                part_tool_name = tool_name
                if not isinstance(part_tool_name, str) and isinstance(part_tool_call_id, str):
                    part_tool_name = tool_names_by_id.get(part_tool_call_id)
                if isinstance(part_tool_name, str) and normalized_part.get("toolName") is None:
                    normalized_part["toolName"] = part_tool_name
            normalized_parts.append(normalized_part)
        if has_tool_result:
            return normalized_parts
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise TanaProxyError(f"tool message is missing tool_call_id/toolCallId: {message!r}")
    result_part: dict[str, Any] = {
        "type": "tool-result",
        "toolCallId": tool_call_id,
        "output": _normalize_tool_result_output(content),
    }
    if not isinstance(tool_name, str):
        tool_name = tool_names_by_id.get(tool_call_id)
    if isinstance(tool_name, str) and tool_name:
        result_part["toolName"] = tool_name
    return [result_part]


def _normalize_tool_result_output(output: Any, is_error: bool = False) -> Any:
    if isinstance(output, Mapping) and isinstance(output.get("type"), str):
        return output
    if isinstance(output, str):
        return {"type": "error-text" if is_error else "text", "value": output}
    if isinstance(output, Sequence) and not isinstance(output, (bytes, bytearray, str)):
        return {"type": "content", "value": [_normalize_content_part(part) for part in output]}
    return {"type": "error-json" if is_error else "json", "value": output}


def _remember_tool_call_names(content: Any, tool_names_by_id: dict[str, str]) -> None:
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray, str)):
        return
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") != "tool-call":
            continue
        tool_call_id = part.get("toolCallId")
        tool_name = part.get("toolName")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            tool_names_by_id[tool_call_id] = tool_name


def _parse_tool_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def _tana_options(model: str, optional_params: Mapping[str, Any], cfg: TanaProxyConfig) -> dict[str, Any]:
    options: dict[str, Any] = {
        "model": model,
        "ignoreLargeContextWarning": cfg.ignore_large_context_warning,
        "ignoreOutOfCreditsWarning": cfg.ignore_out_of_credits_warning,
    }
    _copy_if_set(optional_params, options, "temperature", "temperature")
    _copy_if_set(optional_params, options, "top_p", "topP")
    _copy_if_set(optional_params, options, "frequency_penalty", "frequencyPenalty")
    _copy_if_set(optional_params, options, "presence_penalty", "presencePenalty")
    max_tokens = optional_params.get("max_tokens", optional_params.get("max_completion_tokens"))
    if max_tokens is not None:
        options["maxOutputTokens"] = max_tokens
    stop = optional_params.get("stop")
    if stop is not None:
        options["stopStrings"] = [stop] if isinstance(stop, str) else list(stop)
    provider_options = optional_params.get("provider_options") or optional_params.get("providerOptions")
    if provider_options is not None:
        options["providerOptions"] = provider_options
    return options


def _copy_if_set(source: Mapping[str, Any], dest: dict[str, Any], source_key: str, dest_key: str) -> None:
    value = source.get(source_key)
    if value is not None:
        dest[dest_key] = value


def _copy_first_set(source: Mapping[str, Any], dest: dict[str, Any], source_keys: Sequence[str], dest_key: str) -> None:
    for source_key in source_keys:
        value = source.get(source_key)
        if value is not None:
            dest[dest_key] = value
            return


def _merge_first_set_provider_options(
    source: Mapping[str, Any], dest: dict[str, Any], source_keys: Sequence[str]
) -> None:
    for source_key in source_keys:
        value = source.get(source_key)
        if value is not None:
            _merge_provider_options(dest, value)
            return


def _merge_provider_options(mapping: dict[str, Any], provider_options: Any) -> None:
    if not isinstance(provider_options, Mapping):
        mapping["providerOptions"] = provider_options
        return

    merged = dict(mapping["providerOptions"]) if isinstance(mapping.get("providerOptions"), dict) else {}
    _merge_provider_options_value(merged, provider_options)
    mapping["providerOptions"] = merged


def _merge_provider_options_value(dest: dict[str, Any], provider_options: Mapping[str, Any]) -> None:
    for provider, options in provider_options.items():
        current = dest.get(provider)
        if isinstance(current, Mapping) and isinstance(options, Mapping):
            dest[provider] = {**current, **options}
        else:
            dest[provider] = options


def _move_alias(mapping: dict[str, Any], source_key: str, dest_key: str) -> None:
    value = mapping.pop(source_key, None)
    if value is not None and mapping.get(dest_key) is None:
        mapping[dest_key] = value


def _move_anthropic_cache_control(mapping: dict[str, Any]) -> None:
    cache_control = mapping.pop("cache_control", None)
    if cache_control is None:
        cache_control = mapping.pop("cacheControl", None)
    if cache_control is None:
        return

    provider_options = mapping.get("providerOptions")
    if not isinstance(provider_options, dict):
        provider_options = {}
    anthropic_options = provider_options.get("anthropic")
    if not isinstance(anthropic_options, dict):
        anthropic_options = {}
    anthropic_options["cacheControl"] = cache_control
    provider_options["anthropic"] = anthropic_options
    mapping["providerOptions"] = provider_options


def _parse_tana_response(response: httpx.Response) -> TanaChatResult:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return TanaChatResult(text=response.text)
    data = response.json()
    usage: dict[str, int] | None = None
    if isinstance(data, Mapping):
        raw_usage = data.get("usage")
        if isinstance(raw_usage, Mapping):
            usage = _normalize_usage(raw_usage)
        tool_calls = _list_of_mappings(data.get("toolCalls"))
        tool_results = data.get("toolResults") if isinstance(data.get("toolResults"), list) else None
        if "text" in data:
            return TanaChatResult(
                text=str(data["text"] or ""), tool_calls=tool_calls, tool_results=tool_results, usage=usage, raw=data
            )
        result = data.get("result")
        if isinstance(result, Mapping) and "text" in result:
            return TanaChatResult(text=str(result["text"] or ""), usage=usage, raw=data)
    if isinstance(data, str):
        return TanaChatResult(text=data, usage=usage, raw=data)
    return TanaChatResult(text=json.dumps(data, ensure_ascii=False), usage=usage, raw=data)


def _parse_tana_stream_lines(lines: Iterator[str]) -> Iterator[GenericStreamingChunk]:
    parser = _TanaStreamParser()
    for line in lines:
        yield from parser.parse_line(line)
    yield from parser.finish()


async def _parse_tana_stream_lines_async(lines: AsyncIterator[str]) -> AsyncIterator[GenericStreamingChunk]:
    parser = _TanaStreamParser()
    async for line in lines:
        for chunk in parser.parse_line(line):
            yield chunk
    for chunk in parser.finish():
        yield chunk


def _parse_tana_stream_line(line: str) -> list[GenericStreamingChunk]:
    parser = _TanaStreamParser()
    chunks = parser.parse_line(line)
    chunks.extend(parser.finish())
    return chunks


def _filter_stream_chunks(chunks: Iterator[GenericStreamingChunk]) -> Iterator[GenericStreamingChunk]:
    for chunk in chunks:
        if _is_empty_nonterminal_stream_chunk(chunk):
            continue
        yield chunk
        if _is_terminal_stream_chunk(chunk):
            break


def _is_empty_nonterminal_stream_chunk(chunk: GenericStreamingChunk) -> bool:
    return (
        not _is_terminal_stream_chunk(chunk)
        and chunk.get("text") == ""
        and chunk.get("tool_use") is None
        and chunk.get("usage") is None
        and chunk.get("provider_specific_fields") is None
    )


def _is_terminal_stream_chunk(chunk: GenericStreamingChunk) -> bool:
    return bool(chunk.get("is_finished") or chunk.get("finish_reason"))


class _TanaStreamParser:
    def __init__(self) -> None:
        self._pending_tool_calls: dict[str, dict[str, Any]] = {}
        self._emitted_tool_calls = False
        self._finished = False

    def parse_line(self, line: str) -> list[GenericStreamingChunk]:
        return self._parse_line(line)

    def finish(self) -> list[GenericStreamingChunk]:
        if self._finished:
            return []
        tool_chunks = self._flush_tool_call_chunks()
        if not tool_chunks:
            return []
        self._finished = True
        return [*tool_chunks, _stream_chunk(is_finished=True, finish_reason="tool_calls")]

    def _parse_line(self, line: str) -> list[GenericStreamingChunk]:
        stripped = line.strip()
        if not stripped:
            return []
        if self._finished:
            return []

        if stripped.startswith("data:"):
            payload = stripped.removeprefix("data:").strip()
            if payload == "[DONE]":
                return []
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return []
            if isinstance(data, Mapping):
                return self._stream_chunks_from_event(data)
            return []

        if stripped.startswith(("d:", "e:")):
            try:
                data = json.loads(stripped[2:])
            except json.JSONDecodeError:
                return []
            if not isinstance(data, Mapping):
                return []
            return self._finish_chunks(str(data.get("finishReason") or "stop"), _normalize_stream_usage(data))

        if stripped.startswith("0:"):
            try:
                text = json.loads(stripped[2:])
            except json.JSONDecodeError:
                return []
            if isinstance(text, str) and text:
                return [_stream_chunk(text=text)]

        return []

    def _stream_chunks_from_event(self, data: Mapping[str, Any]) -> list[GenericStreamingChunk]:
        event_type = data.get("type")
        chunks: list[GenericStreamingChunk] = []
        emitted_tools = False

        if event_type == "text-delta":
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                chunks.append(_stream_chunk(text=delta))

        if event_type in {"tool-input-available", "tool-input-error"}:
            self._remember_tool_calls([data])
            emitted_tools = True

        if event_type == "error":
            error_text = data.get("errorText") or data.get("message") or data.get("error")
            raise TanaProxyError(f"Tana streaming error: {error_text or json.dumps(data, ensure_ascii=False)}")

        if event_type == "finish":
            message_metadata = data.get("messageMetadata")
            if isinstance(message_metadata, Mapping):
                self._remember_tool_calls(message_metadata.get("toolCalls"))
                provider_fields: dict[str, Any] = {}
                _copy_if_set(message_metadata, provider_fields, "providerMetadata", "tana_providerMetadata")
                _copy_if_set(message_metadata, provider_fields, "warnings", "tana_warnings")
                _copy_if_set(message_metadata, provider_fields, "response", "tana_response")
                chunks.extend(
                    self._finish_chunks(
                        str(data.get("finishReason") or message_metadata.get("finishReason") or "stop"),
                        _normalize_stream_usage(message_metadata),
                        provider_specific_fields=provider_fields or None,
                    )
                )
            else:
                chunks.extend(
                    self._finish_chunks(str(data.get("finishReason") or "stop"), _normalize_stream_usage(data))
                )
            return chunks

        if not emitted_tools:
            self._remember_tool_calls([data])
        self._remember_tool_calls(data.get("toolCalls"))
        return chunks

    def _remember_tool_calls(self, value: Any) -> None:
        if value is None:
            return
        raw_tool_calls = value if isinstance(value, list) else list(value) if isinstance(value, tuple) else [value]
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, Mapping):
                continue
            normalized = _normalize_tana_tool_call(raw_tool_call)
            if normalized is None:
                continue
            tool_call_id = str(normalized["toolCallId"])
            self._pending_tool_calls[tool_call_id] = _merge_tana_tool_call(
                self._pending_tool_calls.get(tool_call_id), normalized
            )

    def _flush_tool_call_chunks(self) -> list[GenericStreamingChunk]:
        chunks: list[GenericStreamingChunk] = []
        for index, tool_call in enumerate(self._pending_tool_calls.values()):
            chunk = _openai_tool_call_chunk(tool_call, index, streaming_delta=False)
            if chunk is not None:
                chunks.append(_stream_chunk(tool_use=chunk))
        self._pending_tool_calls.clear()
        if chunks:
            self._emitted_tool_calls = True
        return chunks

    def _finish_chunks(
        self, finish_reason: str, usage: dict[str, int] | None, provider_specific_fields: dict[str, Any] | None = None
    ) -> list[GenericStreamingChunk]:
        if self._finished:
            return []
        tool_chunks = self._flush_tool_call_chunks()
        self._finished = True
        return [
            *tool_chunks,
            _stream_chunk(
                is_finished=True,
                finish_reason=_stream_finish_reason(finish_reason, bool(tool_chunks) or self._emitted_tool_calls),
                usage=usage,
                provider_specific_fields=provider_specific_fields,
            ),
        ]


def _merge_tana_tool_call(existing: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in new.items():
        if value is None:
            continue
        if key == "input" and key in merged and _is_empty_json_object(value) and not _is_empty_json_object(merged[key]):
            continue
        merged[key] = value
    if "input" not in merged:
        merged["input"] = {}
    return merged


def _stream_finish_reason(finish_reason: str, has_tool_calls: bool) -> str:
    if has_tool_calls and finish_reason in {"", "stop", "end_turn"}:
        return "tool_calls"
    return finish_reason


def _openai_tool_call_chunk(
    tool_call: Mapping[str, Any], index: int, *, streaming_delta: bool = False
) -> ChatCompletionToolCallChunk | None:
    normalized = _normalize_tana_tool_call(tool_call)
    if normalized is None:
        return None
    arguments = normalized["input"]
    if streaming_delta and _is_empty_json_object(arguments):
        arguments = ""
    elif not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return cast(
        ChatCompletionToolCallChunk,
        {
            "id": str(normalized["toolCallId"]),
            "type": "function",
            "function": {"name": str(normalized["toolName"]), "arguments": arguments},
            "index": index,
        },
    )


def _normalize_tana_tool_call(tool_call: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_function_call = tool_call.get("functionCall")
    function_call: Mapping[str, Any] = raw_function_call if isinstance(raw_function_call, Mapping) else {}
    raw_provider_metadata = tool_call.get("providerMetadata")
    provider_metadata: Mapping[str, Any] = raw_provider_metadata if isinstance(raw_provider_metadata, Mapping) else {}
    raw_provider_options = tool_call.get("providerOptions")
    provider_options: Mapping[str, Any] = raw_provider_options if isinstance(raw_provider_options, Mapping) else {}
    raw_google_metadata = provider_metadata.get("google")
    google_metadata: Mapping[str, Any] = raw_google_metadata if isinstance(raw_google_metadata, Mapping) else {}
    raw_google_options = provider_options.get("google")
    google_options: Mapping[str, Any] = raw_google_options if isinstance(raw_google_options, Mapping) else {}

    tool_call_id = _first_non_none(tool_call.get("toolCallId"), tool_call.get("id"), tool_call.get("tool_call_id"))
    tool_name = _first_non_none(tool_call.get("toolName"), function_call.get("name"), tool_call.get("name"))
    if not tool_call_id or not tool_name:
        return None
    return {
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "input": _first_non_none(tool_call.get("input"), function_call.get("arguments"), tool_call.get("args")),
        "providerExecuted": tool_call.get("providerExecuted"),
        "thoughtSignature": (
            _first_non_none(
                tool_call.get("thoughtSignature"),
                tool_call.get("thought_signature"),
                function_call.get("thoughtSignature"),
                google_metadata.get("thoughtSignature"),
                google_options.get("thoughtSignature"),
            )
        ),
        "thought": (
            _first_non_none(
                tool_call.get("thought"),
                function_call.get("thought"),
                google_metadata.get("thought"),
                google_options.get("thought"),
            )
        ),
    }


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _is_empty_json_object(value: Any) -> bool:
    if value == {}:
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if stripped == "{}":
        return True
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return decoded == {} or (isinstance(decoded, str) and decoded.strip() == "{}")


def _stream_chunk(
    *,
    text: str = "",
    tool_use: ChatCompletionToolCallChunk | None = None,
    is_finished: bool = False,
    finish_reason: str = "",
    usage: dict[str, int] | None = None,
    provider_specific_fields: dict[str, Any] | None = None,
) -> GenericStreamingChunk:
    chunk = GenericStreamingChunk(
        text=text,
        tool_use=tool_use,
        is_finished=is_finished,
        finish_reason=finish_reason,
        usage=cast(Any, usage),
        index=0,
    )
    if provider_specific_fields:
        chunk["provider_specific_fields"] = provider_specific_fields
    return chunk


def _normalize_stream_usage(data: Mapping[str, Any]) -> dict[str, int] | None:
    raw_usage = data.get("usage")
    if isinstance(raw_usage, Mapping):
        return _normalize_usage(raw_usage)
    return _normalize_usage(data)


def _list_of_mappings(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    mappings = [dict(item) for item in value if isinstance(item, Mapping)]
    return mappings or None


def _normalize_usage(usage: Mapping[str, Any]) -> dict[str, int] | None:
    normalized: dict[str, int] = {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens", "inputTokens", "promptTokens"),
        "completion_tokens": ("completion_tokens", "output_tokens", "outputTokens", "completionTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    for dest, keys in aliases.items():
        for key in keys:
            if key in usage and usage[key] is not None:
                normalized[dest] = int(usage[key])
                break
    if "total_tokens" not in normalized and {"prompt_tokens", "completion_tokens"} <= normalized.keys():
        normalized["total_tokens"] = normalized["prompt_tokens"] + normalized["completion_tokens"]
    return normalized or None


def _model_response(model: str, result: TanaChatResult) -> ModelResponse:
    usage = Usage(**cast(Any, result.usage)) if result.usage is not None else None
    tool_calls = _openai_tool_calls(result.tool_calls)
    provider_fields: dict[str, Any] = {}
    if result.tool_results:
        provider_fields["tana_toolResults"] = result.tool_results
    return ModelResponse(
        model=_display_model(model),
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": result.text or None,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                    **({"provider_specific_fields": provider_fields} if provider_fields else {}),
                },
            }
        ],
        usage=usage,
    )


def _openai_tool_calls(tool_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    converted: list[dict[str, Any]] = []
    for index, tool_call in enumerate(tool_calls):
        chunk = _openai_tool_call_chunk(tool_call, index)
        if chunk is None:
            continue
        converted.append({"id": chunk["id"] or f"call_{index}", "type": chunk["type"], "function": chunk["function"]})
    return converted or None


def _body_snippet(response: httpx.Response) -> str:
    body = response.text.strip().replace("\n", " ")
    if len(body) > 500:
        return f"{body[:500]}..."
    return body
