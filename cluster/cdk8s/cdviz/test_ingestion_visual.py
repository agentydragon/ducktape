"""Run the pinned CDviz collector and dashboards against a real TimescaleDB."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.parse import quote, urlsplit

import docker
import httpx
import psycopg
import pytest
import pytest_bazel
from docker.models.containers import Container
from playwright.async_api import Response, async_playwright, expect
from psycopg import sql
from psycopg.rows import dict_row
from python.runfiles import Runfiles

from devinfra.oci.oci import load_oci_image
from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from third_party.containers import cdviz_collector, cdviz_db_migration, grafana_visual, python_3_14_slim
from third_party.containers.timescaledb import timescaledb_postgres
from util.testing.frontend_visual.capture import VisualPage
from util.testing.frontend_visual.context import FROZEN_NOW_MS, deterministic_browser_context

pytest_plugins = ("devinfra.testing.pytest_outputs",)

_REPO = "agentydragon/ducktape"
_TOKEN = "isolated-cdviz-webhook-signing-key"
_GRAFANA_PASSWORD = "isolated-cdviz-grafana-admin"
_OWNER_PASSWORD = "isolated-cdviz-database-owner"
_COLLECTOR_PASSWORD = "isolated-cdviz-collector"
_READER_PASSWORD = "isolated-cdviz-reader"
_FROZEN = datetime.fromtimestamp(FROZEN_NOW_MS / 1000, UTC)


def _stage(name: str) -> None:
    path = undeclared_outputs_dir() / "cdviz-integration-stages.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(name + "\n")


def _diagnostic(message: str) -> None:
    path = undeclared_outputs_dir() / "cdviz-integration-diagnostics.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(message + "\n")


def _url(admin_url: str, database: str, role: str, password: str, host: str | None = None) -> str:
    parsed = urlsplit(admin_url)
    target = host or parsed.hostname or "127.0.0.1"
    port = ":5432" if host else (f":{parsed.port}" if parsed.port else "")
    return f"postgresql://{quote(role)}:{quote(password)}@{target}{port}/{database}?sslmode=disable"


def _timestamp(minute: int, delta: int) -> str:
    return (_FROZEN + timedelta(minutes=minute + delta)).isoformat().replace("+00:00", "Z")


def _setup_database(admin_url: str) -> tuple[str, str, str]:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for role, password in (
            ("cdviz", _OWNER_PASSWORD),
            ("cdviz_collector", _COLLECTOR_PASSWORD),
            ("cdviz_reader", _READER_PASSWORD),
        ):
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
            )
        admin.execute("CREATE DATABASE cdviz OWNER cdviz")

    owner = _url(admin_url, "cdviz", "cdviz", _OWNER_PASSWORD)
    collector = _url(admin_url, "cdviz", "cdviz_collector", _COLLECTOR_PASSWORD)
    reader = _url(admin_url, "cdviz", "cdviz_reader", _READER_PASSWORD)
    # CNPG's Database CR creates TimescaleDB as postgres before migration runs as cdviz.
    parsed = urlsplit(admin_url)
    extension_url = f"postgresql://postgres@{parsed.hostname}:{parsed.port}/cdviz?sslmode=disable"
    with psycopg.connect(extension_url, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION timescaledb")
    return owner, collector, reader


def _run_migration(client: docker.DockerClient, image: str, network: str) -> None:
    name = f"cdviz-migration-{uuid.uuid4().hex[:8]}"
    url = f"postgresql://cdviz:{_OWNER_PASSWORD}@cdviz-db:5432/cdviz?sslmode=disable"
    container = client.containers.run(
        image, ["-database", url, "-path", "/migrations/", "up"], name=name, network=network, detach=True
    )
    path = undeclared_outputs_dir() / f"{name}.log"
    try:
        result = container.wait(timeout=120)
        path.write_bytes(container.logs(stdout=True, stderr=True))
        assert result.get("StatusCode") == 0, f"pinned migration failed; see {path}"
    except BaseException:
        path.write_bytes(container.logs(stdout=True, stderr=True))
        raise
    finally:
        container.remove(force=True)


def _workflow_run(action: str, run_id: int, attempt: int, name: str, conclusion: str | None, minute: int) -> dict:
    run_url = f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}"
    return {
        "action": action,
        "sender": {"login": "cdviz-integration-test"},
        "repository": {"full_name": _REPO, "url": f"https://api.github.com/repos/{_REPO}"},
        "workflow_run": {
            "id": run_id,
            "url": run_url,
            "html_url": f"https://github.com/{_REPO}/actions/runs/{run_id}",
            "name": name,
            "run_attempt": attempt,
            "head_branch": "main",
            "head_sha": f"{run_id:040x}",
            "created_at": _timestamp(minute, -20),
            "run_started_at": _timestamp(minute, -15) if action != "requested" else None,
            "updated_at": _timestamp(minute, 0),
            "conclusion": conclusion,
        },
    }


def _workflow_job(
    action: str, run_id: int, job_id: int, attempt: int, workflow: str, conclusion: str | None, minute: int
) -> dict:
    run_url = f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}"
    job_url = f"https://api.github.com/repos/{_REPO}/actions/jobs/{job_id}"
    return {
        "action": action,
        "sender": {"login": "cdviz-integration-test"},
        "repository": {"full_name": _REPO, "url": f"https://api.github.com/repos/{_REPO}"},
        "workflow_job": {
            "id": job_id,
            "url": job_url,
            "html_url": f"https://github.com/{_REPO}/actions/runs/{run_id}/job/{job_id}",
            "run_url": run_url,
            "run_id": run_id,
            "run_attempt": attempt,
            "workflow_name": workflow,
            "name": "Linux / test",
            "head_branch": "main",
            "head_sha": f"{run_id:040x}",
            "started_at": _timestamp(minute, -10),
            "completed_at": _timestamp(minute, 0) if action == "completed" else None,
            "conclusion": conclusion,
        },
    }


def _post_webhook(base_url: str, event: str, delivery: str, body: dict) -> None:
    raw = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(_TOKEN.encode(), raw, hashlib.sha256).hexdigest()
    response = httpx.post(
        base_url + "/webhook/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + signature,
        },
        timeout=15,
        trust_env=False,
    )
    assert response.status_code < 300, f"webhook returned {response.status_code}: {response.text}"


def _stream_logs(container: Container, path: Path) -> Thread:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = path.open("wb")

    def stream() -> None:
        try:
            for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                output.write(chunk)
                output.flush()
        finally:
            output.close()

    thread = Thread(target=stream, daemon=True)
    thread.start()
    return thread


def _ready(url: str, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2, trust_env=False)
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as error:
            last = str(error)
        time.sleep(0.25)
    raise AssertionError(f"service did not become ready at {url}: {last}")


def _stored_rows(url: str, table: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return connection.execute(sql.SQL("SELECT * FROM cdviz.{}").format(sql.Identifier(table))).fetchall()


def _by_subject(rows: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in rows:
        payload = row.get("last_payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        subject = payload.get("subject") if isinstance(payload, dict) else None
        if isinstance(subject, dict) and isinstance(subject.get("id"), str):
            result.setdefault(subject["id"], []).append(row)
    return result


def _wait_for_rows(reader_url: str, pipeline_ids: set[str], task_ids: set[str]) -> tuple[dict, dict]:
    deadline = time.monotonic() + 60
    pipelines: dict[str, list[dict]] = {}
    tasks: dict[str, list[dict]] = {}
    while time.monotonic() < deadline:
        pipelines = _by_subject(_stored_rows(reader_url, "pipelinerun"))
        tasks = _by_subject(_stored_rows(reader_url, "taskrun"))
        if pipeline_ids.issubset(pipelines) and task_ids.issubset(tasks):
            return pipelines, tasks
        time.sleep(0.5)
    _diagnostic(f"pipeline subjects={sorted(pipelines)}; task subjects={sorted(tasks)}")
    raise AssertionError("collector did not persist all expected workflow run and job subjects")


def _provision_grafana(base_url: str, root: Path) -> None:
    with httpx.Client(base_url=base_url, timeout=20, auth=("admin", _GRAFANA_PASSWORD), trust_env=False) as client:
        response = client.post(
            "/api/datasources",
            json={
                "name": "CI Analytics",
                "uid": "cdviz",
                "type": "grafana-postgresql-datasource",
                "access": "proxy",
                "url": "cdviz-db:5432",
                "user": "cdviz_reader",
                "database": "cdviz",
                "jsonData": {"database": "cdviz", "sslmode": "disable", "postgresVersion": 1800, "timescaledb": True},
                "secureJsonData": {"password": _READER_PASSWORD},
            },
        )
        response.raise_for_status()
        for name in ("pipelines", "tasks"):
            dashboard = json.loads((root / f"ducktape/cluster/cdk8s/cdviz/{name}.json").read_text())
            dashboard["id"] = None
            dashboard["version"] = 0
            response = client.post(
                "/api/dashboards/db", json={"dashboard": dashboard, "overwrite": True, "folderId": 0}
            )
            response.raise_for_status()


async def _capture(
    page,
    outputs: BoundTestOutputs,
    name: str,
    uid: str,
    base_url: str,
    expected_duration: int,
    table_value: str,
    errors: list[str],
    query_results: list[dict],
) -> None:
    tasks: list[asyncio.Task] = []
    pending: set[int] = set()
    duration_values: list[float] = []

    def track_request(request) -> None:
        if "/api/ds/query" in request.url:
            pending.add(id(request))

    def finish_request(request) -> None:
        pending.discard(id(request))

    async def inspect(response: Response) -> None:
        if "/api/ds/query" not in response.url:
            return
        if response.status >= 400:
            errors.append(f"datasource HTTP {response.status}: {response.url}")
            return
        body = await response.json()
        request_body = response.request.post_data_json or {}
        query_by_ref = {query.get("refId"): query.get("rawSql", "") for query in request_body.get("queries", [])}
        for ref_id, value in body.get("results", {}).items():
            if value.get("error"):
                errors.append(f"{name} query {ref_id}: {value['error']}")
            raw_sql = query_by_ref.get(ref_id, "")
            query_results.append({"dashboard": name, "refId": ref_id, "rawSql": raw_sql, **value})
            if "total_duration" in raw_sql:
                for frame in value.get("frames", []):
                    fields = frame.get("schema", {}).get("fields", [])
                    columns = frame.get("data", {}).get("values", [])
                    for field, column in zip(fields, columns, strict=False):
                        if field.get("name") == "total_duration":
                            duration_values.extend(float(item) for item in column if item is not None)

    page.on("request", track_request)
    page.on("requestfinished", finish_request)
    page.on("requestfailed", finish_request)
    page.on("response", lambda response: tasks.append(asyncio.create_task(inspect(response))))
    await page.goto(
        f"{base_url}/d/{uid}/{name}?orgId=1&from={FROZEN_NOW_MS - 31 * 86_400_000}&to={FROZEN_NOW_MS}",
        wait_until="domcontentloaded",
    )
    title = "Pipeline: Executions" if name == "pipelines" else "Task: Executions"
    await expect(page.get_by_text(title, exact=True)).to_be_visible(timeout=30000)
    await expect(page.get_by_text("Total Duration", exact=True).first).to_be_visible(timeout=30000)
    await page.evaluate(
        """async () => {
            const count = Math.floor(document.body.scrollHeight / window.innerHeight);
            for (let i = 0; i < count; i++) {
                window.scrollBy(0, window.innerHeight);
                await new Promise(resolve => setTimeout(resolve, 50));
            }
            window.scrollTo(0, 0);
        }"""
    )
    panel_title = "Pipeline: Latest 20 Executions" if name == "pipelines" else "Task: Latest 20 Executions"
    await expect(page.get_by_text(panel_title, exact=True)).to_be_visible(timeout=30000)
    await page.get_by_text(panel_title, exact=True).click()
    await expect(page.get_by_text(table_value, exact=False).first).to_be_visible(timeout=30000)

    deadline = asyncio.get_running_loop().time() + 45
    while asyncio.get_running_loop().time() < deadline:
        if tasks:
            completed_tasks = tasks[:]
            tasks.clear()
            await asyncio.gather(*completed_tasks)
        if not pending and any(abs(value - expected_duration) < 0.01 for value in duration_values):
            break
        await page.wait_for_timeout(100)
    if tasks:
        await asyncio.gather(*tasks)
    assert not pending, f"{name} dashboard still has pending datasource requests"
    assert any(abs(value - expected_duration) < 0.01 for value in duration_values), (
        f"{name} datasource returned total_duration values {duration_values}, expected {expected_duration} seconds"
    )
    await expect(page.get_by_text("No data", exact=True)).to_have_count(0, timeout=10000)
    with VisualPage(page, outputs=outputs, title=f"CDviz {name} dashboard") as visual:
        await visual.capture(name, full_page=True, animations="disabled")


@pytest.mark.asyncio
async def test_cdviz_webhook_ingestion_and_dashboards(tmp_path: Path, test_outputs: BoundTestOutputs) -> None:
    root = Runfiles.CreateOrRaise().root()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    client = docker.from_env()
    network_name = f"cdviz-it-{uuid.uuid4().hex[:10]}"
    network = client.networks.create(network_name, driver="bridge")
    containers: list[Container] = []
    log_threads: list[Thread] = []
    try:
        _stage("loading pinned runtime images")
        with ThreadPoolExecutor(max_workers=4) as pool:
            collector_future = pool.submit(load_oci_image, cdviz_collector.LAYOUT_RLOCATION)
            migration_future = pool.submit(load_oci_image, cdviz_db_migration.LAYOUT_RLOCATION)
            grafana_future = pool.submit(load_oci_image, grafana_visual.LAYOUT_RLOCATION)
            python_future = pool.submit(load_oci_image, python_3_14_slim.LAYOUT_RLOCATION)
        with timescaledb_postgres(tmp_path, network=network_name, network_alias="cdviz-db") as admin_url:
            _stage("creating roles, database, and Timescale extension")
            owner_url, _, reader_url = _setup_database(admin_url)
            _stage("running pinned migration as non-superuser cdviz")
            _run_migration(client, migration_future.result(), network_name)
            with psycopg.connect(owner_url, autocommit=True) as owner:
                grants = (root / "ducktape/cluster/cdk8s/cdviz/grants.sql").read_text()
                owner.execute(grants)
                owner.execute("CALL cdviz.apply_retention(INTERVAL '90 days')")
            _stage("starting collector and local GitHub polling fixture")

            poll_payload = {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 81004,
                        "url": f"https://api.github.com/repos/{_REPO}/actions/runs/81004",
                        "html_url": f"https://github.com/{_REPO}/actions/runs/81004",
                        "name": "CDviz integration poll",
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "head_sha": f"{81004:040x}",
                        "created_at": (_FROZEN - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                        "run_started_at": (_FROZEN - timedelta(minutes=25)).isoformat().replace("+00:00", "Z"),
                        "updated_at": (_FROZEN - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                        "repository": {"full_name": _REPO, "url": f"https://api.github.com/repos/{_REPO}"},
                    }
                ],
            }
            fixture_dir = runtime / "poll"
            fixture_dir.mkdir()
            (fixture_dir / "payload.json").write_text(json.dumps(poll_payload))
            (fixture_dir / "server.py").write_text(
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "from pathlib import Path\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        body = Path('/fixture/payload.json').read_bytes()\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Type', 'application/json')\n"
                "        self.send_header('Content-Length', str(len(body)))\n"
                "        self.end_headers()\n"
                "        self.wfile.write(body)\n"
                "HTTPServer(('0.0.0.0', 8081), Handler).serve_forever()\n"
            )
            poll_server = client.containers.run(
                python_future.result(),
                command=["python3", "/fixture/server.py"],
                name=f"cdviz-github-fixture-{uuid.uuid4().hex[:8]}",
                network=network_name,
                ports={"8081/tcp": None},
                volumes={str(fixture_dir): {"bind": "/fixture", "mode": "ro"}},
                detach=True,
            )
            containers.append(poll_server)
            log_threads.append(_stream_logs(poll_server, undeclared_outputs_dir() / "cdviz-github-fixture.log"))

            source = root / "ducktape/cluster/cdk8s/cdviz"
            config_dir = runtime / "config"
            config_dir.mkdir()
            config_text = (
                (source / "collector.toml")
                .read_text()
                .replace("https://api.github.com", f"http://{poll_server.name}:8081")
            )
            config_text = config_text.replace('polling_interval = "15m"', 'polling_interval = "1s"')
            (config_dir / "collector.toml").write_text(config_text)
            for name in ("github_webhook.vrl", "github_workflow_runs.vrl"):
                (config_dir / name).write_bytes((source / name).read_bytes())
            secrets = runtime / "secrets"
            (secrets / "database").mkdir(parents=True)
            (secrets / "webhook").mkdir()
            (secrets / "database" / "DATABASE_URL").write_text(
                _url(admin_url, "cdviz", "cdviz_collector", _COLLECTOR_PASSWORD, "cdviz-db")
            )
            (secrets / "webhook" / "token").write_text(_TOKEN)
            state, spool = runtime / "state", runtime / "spool"
            state.mkdir()
            spool.mkdir()
            state.chmod(0o777)
            spool.chmod(0o777)
            collector = client.containers.run(
                collector_future.result(),
                command=["connect", "--config", "/etc/cdviz/collector.toml"],
                name=f"cdviz-collector-{uuid.uuid4().hex[:8]}",
                network=network_name,
                ports={"8080/tcp": ("127.0.0.1", 0)},
                user="1000:1000",
                volumes={
                    str(config_dir): {"bind": "/etc/cdviz", "mode": "ro"},
                    str(secrets): {"bind": "/var/run/secrets", "mode": "ro"},
                    str(state): {"bind": "/var/lib/cdviz/state", "mode": "rw"},
                    str(spool): {"bind": "/var/lib/cdviz/spool", "mode": "rw"},
                },
                read_only=True,
                detach=True,
            )
            containers.append(collector)
            log_threads.append(_stream_logs(collector, undeclared_outputs_dir() / "cdviz-collector.log"))
            collector.reload()
            collector_port = collector.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
            collector_url = f"http://127.0.0.1:{collector_port}"
            _ready(collector_url + "/healthz")
            _ready(collector_url + "/readyz")

            _stage("posting signed success, failure, rerun, and duplicate webhooks")
            scenarios = (
                (81001, 1, "CDviz integration success", "success", 91001, "success", -25),
                (81002, 1, "CDviz integration failure", "failure", 91002, "failure", -21),
                (81003, 1, "CDviz integration rerun", "failure", 91003, "failure", -17),
                (81003, 2, "CDviz integration rerun", "success", 91004, "success", -13),
            )
            for run_id, attempt, workflow, outcome, job_id, job_outcome, minute in scenarios:
                for action in ("requested", "in_progress", "completed"):
                    body = _workflow_run(
                        action, run_id, attempt, workflow, outcome if action == "completed" else None, minute
                    )
                    delivery = f"run-{run_id}-{attempt}-{action}"
                    _post_webhook(collector_url, "workflow_run", delivery, body)
                    if run_id == 81001 and action == "requested":
                        raw = json.dumps(body, separators=(",", ":")).encode()
                        async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
                            rejected = await http.post(
                                collector_url + "/webhook/github",
                                content=raw,
                                headers={
                                    "Content-Type": "application/json",
                                    "X-GitHub-Event": "workflow_run",
                                    "X-GitHub-Delivery": "invalid-signature",
                                    "X-Hub-Signature-256": "sha256=" + ("0" * 64),
                                },
                            )
                        assert rejected.status_code == 401, (
                            f"invalid webhook signature returned {rejected.status_code}, expected 401"
                        )
                    if run_id == 81001 and action == "completed":
                        _post_webhook(collector_url, "workflow_run", delivery, body)
                for action in ("in_progress", "completed"):
                    body = _workflow_job(
                        action,
                        run_id,
                        job_id,
                        attempt,
                        workflow,
                        job_outcome if action == "completed" else None,
                        minute,
                    )
                    _post_webhook(collector_url, "workflow_job", f"job-{job_id}-{action}", body)

            pipeline_ids = {
                f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}/attempts/{attempt}"
                for run_id, attempt in ((81001, 1), (81002, 1), (81003, 1), (81003, 2), (81004, 1))
            }
            task_ids = {
                f"https://api.github.com/repos/{_REPO}/actions/jobs/{job_id}" for job_id in (91001, 91002, 91003, 91004)
            }
            pipeline_rows, task_rows = _wait_for_rows(reader_url, pipeline_ids, task_ids)
            assert len(pipeline_rows) == 5, f"expected five unique run attempts, got {len(pipeline_rows)}"
            assert len(task_rows) == 4, f"expected four unique jobs, got {len(task_rows)}"
            success_id = f"https://api.github.com/repos/{_REPO}/actions/runs/81001/attempts/1"
            failed_id = f"https://api.github.com/repos/{_REPO}/actions/runs/81002/attempts/1"
            rerun_failed_id = f"https://api.github.com/repos/{_REPO}/actions/runs/81003/attempts/1"
            rerun_success_id = f"https://api.github.com/repos/{_REPO}/actions/runs/81003/attempts/2"
            polled_id = f"https://api.github.com/repos/{_REPO}/actions/runs/81004/attempts/1"
            assert len(pipeline_rows[success_id]) == 1, "duplicate delivery created another aggregate row"
            assert len(pipeline_rows[rerun_failed_id]) == 1
            assert len(pipeline_rows[rerun_success_id]) == 1
            assert pipeline_rows[success_id][0]["outcome"] == "success"
            assert pipeline_rows[failed_id][0]["outcome"] == "failure"
            assert pipeline_rows[rerun_failed_id][0]["outcome"] == "failure"
            assert pipeline_rows[rerun_success_id][0]["outcome"] == "success"
            assert pipeline_rows[polled_id][0]["outcome"] == "success"
            _stage("checking reader permissions and database extension")
            with psycopg.connect(reader_url, autocommit=True) as reader:
                count = reader.execute("SELECT count(*) FROM cdviz.cdevents_lake").fetchone()
                assert count is not None
                assert count[0] > 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
                    reader.execute("INSERT INTO cdviz.pipelinerun DEFAULT VALUES")
                assert denied.value.sqlstate == "42501"
            _diagnostic(f"pipeline attempts={len(pipeline_rows)} jobs={len(task_rows)}; reader INSERT denied")

            _stage("starting disposable Grafana and provisioning the real datasource and dashboards")
            grafana = client.containers.run(
                grafana_future.result(),
                name=f"cdviz-grafana-{uuid.uuid4().hex[:8]}",
                network=network_name,
                ports={"3000/tcp": ("127.0.0.1", 0)},
                environment={
                    "GF_ANALYTICS_REPORTING_ENABLED": "false",
                    "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                    "GF_AUTH_ANONYMOUS_ENABLED": "true",
                    "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
                    "GF_AUTH_BASIC_ENABLED": "true",
                    "GF_PLUGINS_PREINSTALL_DISABLED": "true",
                    "GF_SECURITY_ADMIN_PASSWORD": _GRAFANA_PASSWORD,
                    "GF_USERS_DEFAULT_THEME": "dark",
                },
                detach=True,
            )
            containers.append(grafana)
            log_threads.append(_stream_logs(grafana, undeclared_outputs_dir() / "cdviz-grafana.log"))
            grafana.reload()
            grafana_port = grafana.attrs["NetworkSettings"]["Ports"]["3000/tcp"][0]["HostPort"]
            grafana_url = f"http://127.0.0.1:{grafana_port}"
            _ready(grafana_url + "/api/health")
            _provision_grafana(grafana_url, root)

            _stage("running real datasource queries and capturing both dashboard pages")
            errors: list[str] = []
            query_results: list[dict] = []
            async with (
                async_playwright() as playwright,
                deterministic_browser_context(
                    playwright,
                    viewport={"width": 1440, "height": 900},
                    frozen_now_ms=FROZEN_NOW_MS,
                    color_scheme="dark",
                ) as context,
            ):
                for name, uid in (("pipelines", "pipelinerun_executions"), ("tasks", "taskrun_executions")):
                    page = await context.new_page()
                    expected_duration = 4500 if name == "pipelines" else 2400
                    table_value = "CDviz integration success" if name == "pipelines" else "Linux / test"
                    await _capture(
                        page,
                        test_outputs,
                        name,
                        uid,
                        grafana_url,
                        expected_duration,
                        table_value,
                        errors,
                        query_results,
                    )
                    await page.close()
            assert not errors, "; ".join(errors)
            assert query_results, "Grafana did not issue PostgreSQL datasource queries"
            assert any(value.get("frames") for value in query_results), "Grafana returned no query frames"
            _diagnostic(f"Grafana rendered pipelines and tasks dashboards; query frames={len(query_results)}")
    finally:
        for container in reversed(containers):
            with suppress(docker.errors.DockerException):
                container.stop(timeout=5)
            with suppress(docker.errors.DockerException):
                container.remove(force=True)
        for thread in log_threads:
            thread.join(timeout=3)
        network.remove()
        client.close()


if __name__ == "__main__":
    pytest_bazel.main()
