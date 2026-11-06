"""Run the pinned CDviz collector and dashboards against a real TimescaleDB."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.parse import quote, urlencode, urlsplit

import docker
import httpx
import psycopg
import pytest
import pytest_bazel
from docker.models.containers import Container
from playwright.async_api import Route, async_playwright, expect
from psycopg import sql
from psycopg.rows import dict_row
from python.runfiles import Runfiles
from testcontainers.core import testcontainers_config
from testcontainers.core.network import Network
from testcontainers.postgres import PostgresContainer

from cluster.cdk8s.cdviz import feedback_fixture
from devinfra.oci.oci import load_oci_image
from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from grafana.visual_tests.visual_browser import capture_dashboard
from grafana.visual_tests.visual_harness import TEST_PASSWORD, grafana_server
from third_party.containers import cdviz_collector, cdviz_db_migration, python_3_14_slim, ryuk, timescaledb_pg18
from util.testing.frontend_visual.capture import VisualPage
from util.testing.frontend_visual.context import FROZEN_NOW_MS, deterministic_browser_context

pytest_plugins = ("devinfra.testing.pytest_outputs",)

_REPO = "agentydragon/ducktape"
_TOKEN = "isolated-cdviz-webhook-signing-key"
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
        # The upstream image installs Timescale in template1; exercise explicit extension creation as CNPG does.
        admin.execute("CREATE DATABASE cdviz OWNER cdviz TEMPLATE template0")

    owner = _url(admin_url, "cdviz", "cdviz", _OWNER_PASSWORD)
    collector = _url(admin_url, "cdviz", "cdviz_collector", _COLLECTOR_PASSWORD)
    reader = _url(admin_url, "cdviz", "cdviz_reader", _READER_PASSWORD)
    # CNPG's Database CR creates TimescaleDB as postgres before migration runs as cdviz.
    extension_url = _url(admin_url, "cdviz", "postgres", "postgres")
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
            "updated_at": _timestamp(minute, {"requested": -20, "in_progress": -15, "completed": 0}[action]),
            "conclusion": conclusion,
            "pull_requests": [{"number": 123 if run_id == 81001 else 456}],
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


def _ready(url: str, *, container: Container, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        container.reload()
        assert container.status == "running", f"service exited before becoming ready at {url}; see container logs"
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


def _provision_grafana(base_url: str, dashboard_path: Path) -> dict:
    with httpx.Client(base_url=base_url, timeout=20, auth=("admin", TEST_PASSWORD), trust_env=False) as client:
        dashboard: dict = json.loads(dashboard_path.read_text())
        dashboard["id"] = None
        dashboard["version"] = 0
        response = client.post("/api/dashboards/db", json={"dashboard": dashboard, "overwrite": True, "folderId": 0})
        response.raise_for_status()
        return dashboard


async def _capture_dashboard(
    visual: VisualPage,
    *,
    base_url: str,
    dashboard: dict,
    view_name: str,
    query_results: list[dict],
    errors: list[str],
    filtered: bool,
    feedback: bool = False,
) -> None:
    page = visual.page
    panel_by_ref = {
        target["refId"]: str(panel["id"]) for panel in dashboard["panels"] for target in panel.get("targets", [])
    }

    async def inspect(route: Route) -> None:
        request_body = route.request.post_data_json
        assert request_body is not None
        response = await route.fetch()
        if response.status >= 400:
            detail = await response.text()
            errors.append(f"{view_name} datasource HTTP {response.status}: {detail}")
            query_results.append(
                {"view": view_name, "request": request_body, "status": response.status, "error": detail}
            )
            await route.fulfill(response=response)
            return
        body = await response.json()
        request_by_ref = {query["refId"]: query for query in request_body.get("queries", [])}
        for ref_id, result in body.get("results", {}).items():
            query = request_by_ref.get(ref_id, {})
            raw_sql = query.get("rawSql", "")
            if result.get("error"):
                errors.append(f"{view_name} query {ref_id}: {result['error']}")
            if _UNRESOLVED_DASHBOARD_VARIABLE.search(raw_sql):
                errors.append(f"{view_name} query {ref_id} contains an unresolved dashboard variable")
            query_results.append(
                {
                    "view": view_name,
                    "panel_id": panel_by_ref.get(ref_id, ""),
                    "ref_id": ref_id,
                    "raw_sql": raw_sql,
                    "request": query,
                    "result": result,
                }
            )
        await route.fulfill(response=response)

    await page.route("**/api/ds/query*", inspect)
    params = {"orgId": "1", "from": str(FROZEN_NOW_MS - 3 * 3_600_000), "to": str(FROZEN_NOW_MS)}
    if feedback:
        params.update({"var-scope": "PR", "var-clock": "PR head event" if filtered else "all"})
        if filtered:
            params["var-bucket"] = "5 minutes"
    elif filtered:
        params.update(
            {"var-workflow": "agentydragon/ducktape/CDviz integration success", "var-branch": "main", "var-pr": "123"}
        )
    await page.goto(
        f"{base_url}/d/{dashboard['uid']}/ci-workflow-and-job-timings?{urlencode(params)}",
        wait_until="domcontentloaded",
    )
    first_panel = next(panel for panel in dashboard["panels"] if panel.get("targets"))
    await expect(page.get_by_text(first_panel["title"], exact=True).first).to_be_visible(timeout=30000)
    await capture_dashboard(visual, checkpoint=view_name)
    await expect(page.get_by_text("No data", exact=True)).to_have_count(0, timeout=10000)
    if feedback:
        bucket = "5m" if filtered else "1h"
        await expect(page.get_by_text(f"First-verdict latency ({bucket} buckets)", exact=True)).to_be_visible()
    if feedback and filtered:
        repaired_then_failed = page.get_by_role("row").filter(has=page.get_by_text("207", exact=True))
        await expect(repaired_then_failed.get_by_text("6.0 mins", exact=True)).to_be_visible()
        await expect(repaired_then_failed.get_by_text("4.0 mins", exact=True)).to_have_count(2)


_UNRESOLVED_DASHBOARD_VARIABLE = re.compile(
    r"\$\{(?:workflow|branch|pr|scope|clock|bucket)(?::[^}]+)?\}|\$(?:workflow|branch|pr|scope|clock|bucket)\b"
)


def _panel_results(query_results: list[dict], view_name: str, dashboard: dict) -> dict[str, dict]:
    results = {
        result["panel_id"]: result for result in query_results if result["view"] == view_name and result.get("panel_id")
    }
    expected_panel_ids = {str(panel["id"]) for panel in dashboard["panels"] if panel.get("targets")}
    assert expected_panel_ids.issubset(results), (
        f"{view_name} missing datasource responses for panels {sorted(expected_panel_ids - results.keys())}; "
        f"received {sorted(results)}"
    )
    for panel_id in expected_panel_ids:
        frames = results[panel_id]["result"].get("frames", [])
        assert _frame_rows(frames), f"{view_name} panel {panel_id} returned no nonempty frames"
    return results


def _frame_rows(frames: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for frame in frames:
        fields = frame.get("schema", {}).get("fields", [])
        columns = frame.get("data", {}).get("values", [])
        if not fields or len(fields) != len(columns):
            continue
        rows.extend(
            {field["name"]: value for field, value in zip(fields, values, strict=True)}
            for values in zip(*columns, strict=True)
        )
    return rows


def _field_values(panel_result: dict, name: str) -> list:
    for frame in panel_result["result"].get("frames", []):
        fields = frame.get("schema", {}).get("fields", [])
        columns = frame.get("data", {}).get("values", [])
        for field, values in zip(fields, columns, strict=True):
            if field.get("name") == name:
                return list(values)
    raise AssertionError(f"datasource response has no field {name!r}")


def _assert_number(panel_result: dict, name: str, expected: float) -> None:
    values = _field_values(panel_result, name)
    assert len(values) == 1
    assert math.isclose(float(values[0]), expected, rel_tol=0, abs_tol=0.01), (
        f"expected {name}={expected}, got {values}"
    )


def _assert_dashboard_responses(query_results: list[dict], dashboard: dict, view_name: str, *, filtered: bool) -> None:
    panels = _panel_results(query_results, view_name, dashboard)
    _assert_number(panels["1"], "attempts", 1 if filtered else 5)
    _assert_number(panels["2"], "failure_rate", 0 if filtered else 40)
    for panel_id, field in (("3", "p50_runtime"), ("4", "p95_runtime")):
        _assert_number(panels[panel_id], field, 900)

    runtime_rows = _frame_rows(panels["5"]["result"]["frames"])
    assert all(math.isclose(float(row["P50"]), 900, abs_tol=0.01) for row in runtime_rows)
    assert all(math.isclose(float(row["P95"]), 900, abs_tol=0.01) for row in runtime_rows)
    outcome_rows = _frame_rows(panels["6"]["result"]["frames"])
    assert outcome_rows
    expected_success, expected_failure = (1, 0) if filtered else (3, 2)
    assert sum(int(row["Success"]) for row in outcome_rows) == expected_success
    assert sum(int(row["Failure"]) for row in outcome_rows) == expected_failure

    workflow_rows = _frame_rows(panels["7"]["result"]["frames"])
    job_rows = _frame_rows(panels["8"]["result"]["frames"])
    success_runs = [row for row in workflow_rows if row["Workflow"] == "CDviz integration success"]
    success_jobs = [
        row
        for row in job_rows
        if row["Workflow"] == "CDviz integration success"
        and row["Job"] == "Linux / test"
        and row["Outcome"] == "success"
    ]
    if filtered:
        assert len(workflow_rows) == len(success_runs) == 1
        assert workflow_rows[0]["PR"] == "123"
        assert workflow_rows[0]["Branch"] == "main"
        assert workflow_rows[0]["Attempt"] == "1"
        assert workflow_rows[0]["Outcome"] == "success"
        assert math.isclose(float(workflow_rows[0]["Runtime"]), 900, abs_tol=0.01)
        assert len(job_rows) == len(success_jobs) == 1
        assert job_rows[0]["Branch"] == "main"
        assert math.isclose(float(job_rows[0]["Runtime"]), 600, abs_tol=0.01)
    else:
        assert len(workflow_rows) == 5
        assert len(job_rows) == 4
        assert len(success_runs) == 1
        assert success_runs[0]["PR"] == "123"
        assert success_runs[0]["Branch"] == "main"
        assert success_runs[0]["Outcome"] == "success"
        assert math.isclose(float(success_runs[0]["Runtime"]), 900, abs_tol=0.01)
        assert len(success_jobs) == 1
        assert all(math.isclose(float(row["Runtime"]), 600, abs_tol=0.01) for row in job_rows)


@pytest.mark.asyncio
async def test_cdviz_webhook_ingestion_and_dashboards(tmp_path: Path, test_outputs: BoundTestOutputs) -> None:
    root = Runfiles.CreateOrRaise().root()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    client = docker.from_env()
    network = Network()
    network_created = False
    containers: list[Container] = []
    log_threads: list[Thread] = []
    try:
        _stage("loading pinned runtime images")
        with ThreadPoolExecutor(max_workers=4) as pool:
            collector_future = pool.submit(load_oci_image, cdviz_collector.LAYOUT_RLOCATION)
            migration_future = pool.submit(load_oci_image, cdviz_db_migration.LAYOUT_RLOCATION)
            python_future = pool.submit(load_oci_image, python_3_14_slim.LAYOUT_RLOCATION)
            ryuk_future = pool.submit(load_oci_image, ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
        ryuk_future.result()
        network.create()
        network_created = True
        network_name = network.name
        with (
            PostgresContainer(
                image=load_oci_image(timescaledb_pg18.LAYOUT_RLOCATION),
                username="postgres",
                password="postgres",
                dbname="postgres",
            )
            .with_network(network)
            .with_network_aliases("cdviz-db")
            .with_env("NO_TS_TUNE", "true")
            .with_command("postgres -c timescaledb.telemetry_level=off -c max_locks_per_transaction=128")
        ) as postgres:
            log_threads.append(
                _stream_logs(postgres.get_wrapped_container(), undeclared_outputs_dir() / "cdviz-db.log")
            )
            admin_url = (
                f"postgresql://postgres:postgres@{postgres.get_container_host_ip()}:"
                f"{postgres.get_exposed_port(5432)}/postgres"
            )
            _stage("creating roles, database, and Timescale extension")
            owner_url, _, reader_url = _setup_database(admin_url)
            _stage("running pinned migration as non-superuser cdviz")
            _run_migration(client, migration_future.result(), network_name)
            with psycopg.connect(owner_url, autocommit=True) as owner:
                owner.execute((root / "ducktape/cluster/cdk8s/cdviz/001_ci_feedback.sql").read_text())
                grants = (root / "ducktape/cluster/cdk8s/cdviz/grants.sql").read_text()
                owner.execute(grants)
                owner.execute("CALL cdviz.apply_retention(INTERVAL '90 days')")
            _stage("starting collector and local GitHub polling fixture")

            poll_payload: dict = {
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
            # A full first page of distinct, recently queued runs puts the completed
            # fixture on page two. Recent runs fall outside the frozen dashboard range.
            recent_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            recent_runs = [
                {
                    **poll_payload["workflow_runs"][0],
                    "id": run_id,
                    "url": f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}",
                    "html_url": f"https://github.com/{_REPO}/actions/runs/{run_id}",
                    "name": "Recent queued run",
                    "head_sha": f"{run_id:040x}",
                    "status": "queued",
                    "conclusion": None,
                    "created_at": recent_at,
                    "run_started_at": None,
                    "updated_at": recent_at,
                }
                for run_id in range(90000, 90100)
            ]
            (fixture_dir / "recent.json").write_text(json.dumps({"total_count": 101, "workflow_runs": recent_runs}))
            shutil.copyfile(root / "ducktape/cluster/cdk8s/cdviz/github_api_server.py", fixture_dir / "server.py")
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
            config_text = config_text.replace('polling_interval = "15m"', 'polling_interval = "5s"')
            (config_dir / "collector.toml").write_text(config_text)
            for name in ("github_webhook.vrl", "github_workflow_runs.vrl", "github_policy.vrl"):
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
            _ready(collector_url + "/healthz", container=collector)
            _ready(collector_url + "/readyz", container=collector)

            _stage("posting signed success, failure, rerun, and duplicate webhooks")
            scenarios = (
                (81001, 1, "CDviz integration success", "success", 91001, "success", -155),
                (81002, 1, "CDviz integration failure", "failure", 91002, "failure", -125),
                (81003, 1, "CDviz integration rerun", "failure", 91003, "failure", -85),
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
            pipeline_ids.update(
                f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}/attempts/1"
                for run_id in range(90000, 90100)
            )
            task_ids = {
                f"https://api.github.com/repos/{_REPO}/actions/jobs/{job_id}" for job_id in (91001, 91002, 91003, 91004)
            }
            pipeline_rows, task_rows = _wait_for_rows(reader_url, pipeline_ids, task_ids)
            assert len(pipeline_rows) == 105, f"expected 105 unique run attempts, got {len(pipeline_rows)}"
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
            poll_server.reload()
            poll_port = poll_server.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
            async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
                response = await http.get(f"http://127.0.0.1:{poll_port}/requests")
                response.raise_for_status()
                pages = response.json()
            assert set(pages) == {1, 2}, f"pagination must follow page two and stop at the age cutoff: {pages}"
            _stage("checking reader permissions and database extension")
            with psycopg.connect(reader_url, autocommit=True) as reader:
                count = reader.execute("SELECT count(*) FROM cdviz.cdevents_lake").fetchone()
                assert count is not None
                assert count[0] > 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
                    reader.execute("INSERT INTO cdviz.cdevents_lake DEFAULT VALUES")
                assert denied.value.sqlstate == "42501"
            _diagnostic(f"pipeline attempts={len(pipeline_rows)} jobs={len(task_rows)}; reader INSERT denied")

            dashboard_path = root / "ducktape/cluster/cdk8s/cdviz/dashboard.json"
            errors: list[str] = []
            query_results: list[dict] = []
            diagnostics_path = undeclared_outputs_dir() / "cdviz-grafana-queries.json"
            with grafana_server(
                network,
                [
                    {
                        "name": "CI Analytics",
                        "uid": "cdviz",
                        "type": "grafana-postgresql-datasource",
                        "access": "proxy",
                        "url": "cdviz-db:5432",
                        "user": "cdviz_reader",
                        "database": "cdviz",
                        "jsonData": {
                            "database": "cdviz",
                            "sslmode": "disable",
                            "postgresVersion": 1800,
                            "timescaledb": True,
                        },
                        "secureJsonData": {"password": _READER_PASSWORD},
                    }
                ],
            ) as grafana_url:
                dashboard = _provision_grafana(grafana_url, dashboard_path)
                _stage("querying and capturing all-runs and filtered dashboard views")
                try:
                    async with (
                        async_playwright() as playwright,
                        deterministic_browser_context(
                            playwright,
                            viewport={"width": 1440, "height": 1200},
                            frozen_now_ms=FROZEN_NOW_MS,
                            color_scheme="dark",
                        ) as context,
                    ):
                        for view_name, filtered in (("all-runs", False), ("filtered", True)):
                            page = await context.new_page()
                            with VisualPage(
                                page, outputs=test_outputs, title=f"CDviz CI dashboard ({view_name})"
                            ) as visual:
                                await _capture_dashboard(
                                    visual,
                                    base_url=grafana_url,
                                    dashboard=dashboard,
                                    view_name=view_name,
                                    query_results=query_results,
                                    errors=errors,
                                    filtered=filtered,
                                )
                            await page.close()
                    assert not errors, "; ".join(errors)
                    _assert_dashboard_responses(query_results, dashboard, "all-runs", filtered=False)
                    _assert_dashboard_responses(query_results, dashboard, "filtered", filtered=True)
                    _diagnostic(f"Grafana rendered all 8 panels in both views; captured {len(query_results)} responses")
                    with psycopg.connect(owner_url, autocommit=True) as owner:
                        # The policy poll uses collector time; deterministic dashboard
                        # histories use the browser fixture's frozen source clock.
                        policy = owner.execute(
                            "SELECT payload #> '{customData,ci_policy,contexts}' FROM cdviz.cdevents_lake "
                            "WHERE payload #> '{customData,ci_policy}' IS NOT NULL LIMIT 1"
                        ).fetchone()
                        assert policy is not None
                        assert set(policy[0]) == set(feedback_fixture.CONTEXTS)
                        feedback_fixture.seed(owner, _FROZEN)
                        feedback_fixture.assert_unknown_histories(owner, _FROZEN)
                    with psycopg.connect(reader_url) as reader:
                        feedback_fixture.assert_histories(reader, _FROZEN)
                    feedback = _provision_grafana(grafana_url, source / "feedback-dashboard.json")
                    async with (
                        async_playwright() as playwright,
                        deterministic_browser_context(
                            playwright,
                            viewport={"width": 1440, "height": 1200},
                            frozen_now_ms=FROZEN_NOW_MS,
                            color_scheme="dark",
                        ) as context,
                    ):
                        for view_name, filtered in (("feedback", False), ("feedback-head-clock", True)):
                            page = await context.new_page()
                            with VisualPage(
                                page, outputs=test_outputs, title=f"Commit to CI feedback ({view_name})"
                            ) as visual:
                                await _capture_dashboard(
                                    visual,
                                    base_url=grafana_url,
                                    dashboard=feedback,
                                    view_name=view_name,
                                    query_results=query_results,
                                    errors=errors,
                                    filtered=filtered,
                                    feedback=True,
                                )
                            await page.close()
                            panels = _panel_results(query_results, view_name, feedback)
                            bucket_times = sorted({row["Time"] for row in _frame_rows(panels["4"]["result"]["frames"])})
                            offsets = (160, 130, 40, 30, 20) if filtered else (180, 60)
                            assert bucket_times == [FROZEN_NOW_MS - minutes * 60_000 for minutes in offsets]
                            if filtered:
                                _assert_number(panels["2"], "n", 5)
                                _assert_number(panels["2"], "p50_seconds", 240)
                                thresholds = _frame_rows(panels["6"]["result"]["frames"])
                                assert [row["eligible_heads"] for row in thresholds] == [12, 12, 10]
                                assert [row["still_observing"] for row in thresholds] == [1, 1, 3]
                                assert [row["heads_green"] for row in thresholds] == [3, 4, 4]
                    assert not errors, "; ".join(errors)
                finally:
                    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                    diagnostics_path.write_text(json.dumps({"errors": errors, "queries": query_results}, indent=2))
    finally:
        for container in reversed(containers):
            with suppress(docker.errors.DockerException):
                container.stop(timeout=5)
            with suppress(docker.errors.DockerException):
                container.remove(force=True)
        for thread in log_threads:
            thread.join(timeout=3)
        if network_created:
            network.remove()
        client.close()


if __name__ == "__main__":
    pytest_bazel.main()
