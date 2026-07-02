"""Dispatcher: classifier-gated dispatch of worker Jobs into zone namespaces.

The one bespoke service in the multi-agent dispatch plane
(haku/plans/multi_agent.md → new-code inventory). POST /jobs = lint + classifier
+ per-job key mint + Job stamp; results flow back worker→validator→Postgres.

Haku reads the jobs/results tables directly with a read-only Postgres role
(operator, 2026-07-02 — deletes the whole GET surface here and gives Haku full
SQL filtering for free); this API keeps only the operations that act on
k8s/LiteLLM: POST /jobs (gate + stamp), POST /jobs/<id>/result (worker
turn-in), DELETE /jobs/<id> (kill switch). The dispatcher holds no haku-state
credential.
"""

import asyncio
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast

import uvicorn
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.exc import IntegrityError

from haku.dispatch import db, k8s_jobs, prompt_lint, result_tokens
from haku.dispatch.classifier import ClassifyFn, make_classifier
from haku.dispatch.config import ZONE_MODELS, ZONE_NAMESPACES, Settings
from haku.dispatch.litellm_keys import LiteLLMKeyClient
from haku.dispatch.models import (
    ClassifierVerdict,
    JobRecord,
    JobRequest,
    JobStatus,
    RejectionResponse,
    ResultSubmission,
)

logger = logging.getLogger(__name__)

_bearer = HTTPBearer()


@dataclass
class AppResources:
    """Backends the endpoints act through; tests inject fakes."""

    sessionmaker: db.SessionMaker
    stamper: k8s_jobs.ZoneJobStamper
    keys: LiteLLMKeyClient
    classify: ClassifyFn


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _resources(request: Request) -> AppResources:
    return cast(AppResources, request.app.state.resources)


class PromptRejectedError(Exception):
    """Gate refusal — rendered as a 403 RejectionResponse (see the handler in
    create_app), so the OpenAPI-declared 403 shape matches the wire exactly."""

    def __init__(self, verdict: ClassifierVerdict) -> None:
        self.verdict = verdict


async def _require_haku(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    settings: Annotated[Settings, Depends(_settings)],
) -> None:
    if not hmac.compare_digest(credentials.credentials, settings.haku_api_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def create_app(settings: Settings, resources: AppResources) -> FastAPI:
    app = FastAPI(title="dispatcher")
    app.state.settings = settings
    app.state.resources = resources

    @app.exception_handler(PromptRejectedError)
    async def prompt_rejected_handler(request: Request, exc: PromptRejectedError) -> JSONResponse:
        return JSONResponse(
            status_code=403, content=RejectionResponse(detail="prompt rejected", verdict=exc.verdict).model_dump()
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/jobs", dependencies=[Depends(_require_haku)], responses={403: {"model": RejectionResponse}})
    async def create_job(
        request: JobRequest,
        settings: Annotated[Settings, Depends(_settings)],
        res: Annotated[AppResources, Depends(_resources)],
    ) -> JobRecord:
        if request.model not in ZONE_MODELS[request.zone]:
            raise HTTPException(status_code=422, detail=f"model {request.model!r} not in zone {request.zone} allowlist")

        name = k8s_jobs.job_name(request.idempotency_key)
        namespace = ZONE_NAMESPACES[request.zone]

        async with res.sessionmaker() as session:
            if (existing := await db.get_job(session, name)) is not None:
                return db.to_record(existing)

        if found := prompt_lint.find_credentials(request.prompt):
            raise PromptRejectedError(
                ClassifierVerdict(allowed=False, reason=f"prompt contains credential material: {', '.join(found)}")
            )
        verdict = await res.classify(request.zone, request.prompt)
        if not verdict.allowed:
            raise PromptRejectedError(verdict)

        # A pre-existing k8s Job without a DB row means a previous attempt died
        # between stamping and inserting — adopt it instead of re-stamping.
        if not await res.stamper.job_exists(namespace, name):
            litellm_key = await res.keys.mint(
                name, sorted(ZONE_MODELS[request.zone]), request.max_budget_usd, settings.job_key_ttl
            )
            await res.stamper.create(
                name=name,
                namespace=namespace,
                zone=request.zone,
                model=request.model,
                prompt=request.prompt,
                litellm_key=litellm_key,
                result_token=result_tokens.mint(settings.result_token_secret, name),
            )

        record = JobRecord(
            id=name,
            zone=request.zone,
            model=request.model,
            status=JobStatus.CREATED,
            prompt=request.prompt,
            created_at=datetime.now(UTC),
            completed_at=None,
            exit_code=None,
            result=None,
        )
        async with res.sessionmaker() as session:
            try:
                await db.insert_job(session, record)
            except IntegrityError:
                # Concurrent POST with the same idempotency key won the insert;
                # its row is authoritative. (The doubly-minted LiteLLM key is an
                # accepted residual: budget- and TTL-bounded, and Haku is the
                # single caller in practice.)
                await session.rollback()
                existing = await db.get_job(session, name)
                assert existing is not None
                return db.to_record(existing)
        return record

    @app.delete("/jobs/{job_id}", dependencies=[Depends(_require_haku)])
    async def kill_job(job_id: str, res: Annotated[AppResources, Depends(_resources)]) -> JobRecord:
        async with res.sessionmaker() as session:
            if (row := await db.get_job(session, job_id)) is None:
                raise HTTPException(status_code=404, detail="no such job")
            await res.stamper.delete(ZONE_NAMESPACES[row.zone], job_id)
            await res.keys.revoke(job_id)
            await db.mark_killed(session, row)
            return db.to_record(row)

    @app.post("/jobs/{job_id}/result")
    async def submit_result(
        job_id: str,
        submission: ResultSubmission,
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
        settings: Annotated[Settings, Depends(_settings)],
        res: Annotated[AppResources, Depends(_resources)],
    ) -> JobRecord:
        if not result_tokens.verify(settings.result_token_secret, job_id, credentials.credentials):
            raise HTTPException(status_code=401, detail="unauthorized")
        async with res.sessionmaker() as session:
            if (row := await db.get_job(session, job_id)) is None:
                raise HTTPException(status_code=404, detail="no such job")
            if row.result is not None:
                return db.to_record(row)
            await db.store_result(session, row, submission.result, submission.exit_code)
            return db.to_record(row)

    return app


async def _serve(settings: Settings) -> None:
    engine = db.make_engine(settings.database_url)
    await db.create_schema(engine)
    k8s_config.load_incluster_config()
    api_client = k8s_client.ApiClient()
    keys = LiteLLMKeyClient(settings.workers_litellm_url, settings.workers_litellm_master_key)
    resources = AppResources(
        sessionmaker=db.make_sessionmaker(engine),
        stamper=k8s_jobs.ZoneJobStamper(api_client, settings.job_template_path.read_text()),
        keys=keys,
        classify=make_classifier(
            AsyncAnthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url),
            settings.classifier_model,
        ),
    )
    try:
        config = uvicorn.Config(create_app(settings, resources), host=settings.host, port=settings.port)
        await uvicorn.Server(config).serve()
    finally:
        await keys.aclose()
        await api_client.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve(Settings()))


if __name__ == "__main__":
    main()
