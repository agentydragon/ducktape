from __future__ import annotations

import asyncio
import contextlib
import logging

from openai import AsyncOpenAI

from .config import EmberSettings
from .history import ConversationHistory
from .matrix_client import MatrixClient
from .object_store import ObjectStoreClient
from .openai_agent import OpenAIAgent
from .runtime.python_session import ensure_kernel as ensure_python_kernel

logger = logging.getLogger(__name__)


class EmberRuntime:
    def __init__(self, settings: EmberSettings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._object_store: ObjectStoreClient | None = None
        self._initialise_components()

    async def start(self) -> None:
        logger.info("Starting pilot runtime")
        await self._matrix_client.start()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="pilot-runtime-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._matrix_client.close()
        await self._openai_client.close()
        logger.info("Pilot runtime stopped")

    async def restart(self) -> None:
        await self.stop()
        self._initialise_components()
        await self.start()

    def _initialise_components(self) -> None:
        self._history = ConversationHistory(self._settings.history_path)
        self._matrix_client = MatrixClient(self._settings.matrix)
        self._settings.workspace_path.mkdir(parents=True, exist_ok=True)
        self._openai_client = self._create_openai_client()
        self._object_store = self._create_object_store_client()
        self._agent = OpenAIAgent(
            self._settings.openai,
            self._history,
            self._openai_client,
            self._matrix_client,
            self._settings.workspace_path,
            self._object_store,
        )
        ensure_python_kernel()

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                if not (events := await self._matrix_client.get_events(timeout=60.0)):
                    continue
                message_text = "\n".join(f"{event.sender}: {event.body}" for event in events)
                logger.info("Received Matrix batch:\n%s", message_text)
                room_ids = {event.room_id for event in events if event.room_id}

                if room_ids:
                    await self._matrix_client.set_typing(room_ids, True)
                try:
                    await self._refresh_openai_client()
                    await self._agent.handle_user_message(message_text)
                finally:
                    if room_ids:
                        await self._matrix_client.set_typing(room_ids, False)
        except asyncio.CancelledError:
            raise

    async def _refresh_openai_client(self) -> None:
        api_key = self._settings.openai.api_key_secret.value(required=True)
        self._openai_client.api_key = api_key
        if self._settings.openai.api_base:
            self._openai_client.base_url = self._settings.openai.api_base

    def _create_openai_client(self) -> AsyncOpenAI:
        api_key = self._settings.openai.api_key_secret.value(required=True)
        return AsyncOpenAI(api_key=api_key, base_url=self._settings.openai.api_base)

    def _create_object_store_client(self) -> ObjectStoreClient | None:
        settings = self._settings.object_store
        if settings is None:
            return None
        return ObjectStoreClient(settings)
