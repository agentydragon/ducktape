from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
from openai._exceptions import APIStatusError
from platformdirs import user_cache_dir

from .core import family_of

if TYPE_CHECKING:
    from .main import ProbeResult


def cache_dir() -> Path:
    return Path(user_cache_dir(appname="adgn-llm", appauthor=False)) / "openai_probe"


def cache_file() -> Path:
    return cache_dir() / "history.jsonl"


def ensure_cache_dir() -> None:
    cache_dir().mkdir(parents=True, exist_ok=True)


def persist_result(res: ProbeResult) -> None:
    ensure_cache_dir()
    record = res.to_cache_record()
    if not cache_file().exists():
        cache_file().write_text("", encoding="utf-8")
    with cache_file().open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")


def persist_models_listing(lines: list[str]) -> None:
    if not lines:
        return
    ensure_cache_dir()
    with cache_file().open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


DB_HOST = os.getenv("DB_HOST", "timescaledb.observability.svc.cluster.local")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "openai_probe")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

_db_pool: asyncpg.Pool | None = None


async def get_db_pool() -> asyncpg.Pool:
    global _db_pool  # noqa: PLW0603
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD, min_size=1, max_size=10
        )
    return _db_pool


async def write_probe_result(res: ProbeResult) -> None:
    # Expect a ProbeResult-like object with attributes, not dynamic getattr probing
    if not res.started_at or not res.ended_at:
        return

    pool = await get_db_pool()

    chat_json: Any | None = None
    responses_json: Any | None = None

    if res.ok and res.raw is not None:
        if res.kind == "chat":
            chat_obj = res.raw_chat()
            if chat_obj is not None:
                chat_json = chat_obj.model_dump(exclude_none=False)
        elif res.kind == "responses":
            resp_obj = res.raw_responses()
            if resp_obj is not None:
                responses_json = resp_obj.model_dump(exclude_none=False)

    error_code = None
    error_status = None
    error_body_json: Any | None = None
    if not res.ok and res.exc is not None:
        if isinstance(res.exc, APIStatusError):
            error_status = res.exc.status_code
            resp = res.exc.response
            if resp is not None:
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        error_body_json = parsed
                except ValueError:
                    # Non-JSON body; ignore
                    pass
            if error_body_json is None and isinstance(res.exc.body, dict):
                error_body_json = res.exc.body
        classification = res.error_classification
        if classification:
            error_code = classification[0].value

    request_id = None
    if isinstance(res.exc, APIStatusError):
        request_id = res.exc.request_id
    elif res.ok:
        if isinstance(chat_json, dict) and chat_json.get("id"):
            request_id = str(chat_json.get("id"))
        elif isinstance(responses_json, dict) and responses_json.get("id"):
            request_id = str(responses_json.get("id"))

    insert_sql = """
    INSERT INTO probe_results (
        start_time, end_time, latency_s, model, family, kind, success,
        error_code, error_status, error_body,
        chat_response,
        responses_body,
        request_id, api_key_suffix
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7,
        $8, $9, $10::jsonb,
        $11::jsonb,
        $12::jsonb, $13, $14
    )
    """

    async with pool.acquire() as conn:
        eb = json.dumps(error_body_json) if error_body_json is not None else None
        cj = json.dumps(chat_json) if chat_json is not None else None
        rj = json.dumps(responses_json) if responses_json is not None else None
        await conn.execute(
            insert_sql,
            res.started_at,
            res.ended_at,
            res.latency_s,
            res.model_id,
            family_of(res.model_id).value,
            res.kind,
            res.ok,
            error_code,
            error_status,
            eb,
            cj,
            rj,
            request_id,
            None,
        )
