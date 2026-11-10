"""Adversarial CI histories for the PostgreSQL/Grafana integration test."""

import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPOSITORY = "agentydragon/ducktape"
CONTEXTS = ("bazel-ci / Test & Build", "Pre-commit checks", "Build artifacts + imports check")
WORKFLOWS = ("CI", "Pre-commit checks", "Nix wheel check")


def seed(connection: psycopg.Connection, now: datetime) -> None:
    def write(at: datetime, github: dict, *, predicate: str = "finished", policy: dict | None = None) -> None:
        payload: dict = {
            "context": {"timestamp": at.isoformat()},
            "subject": {"id": str(uuid4())},
            "customData": {"github": {"repository": {"full_name": REPOSITORY}, **github}},
        }
        if policy is not None:
            payload["customData"]["ci_policy"] = policy
        connection.execute(
            "INSERT INTO cdviz.cdevents_lake (timestamp, payload, subject, predicate, context_id) "
            "VALUES (%s, %s, 'taskrun', %s, %s)",
            (at, Jsonb(payload), predicate, str(uuid4())),
        )

    write(
        now - timedelta(hours=4),
        {},
        policy={"repository": REPOSITORY, "branch": "devel", "contexts": CONTEXTS, "supported": True},
    )

    def head(number: int, age: int, revision: int = 0) -> tuple[str, datetime]:
        sha = hashlib.sha256(f"{number}/{revision}".encode()).hexdigest()[:40]
        at = now - timedelta(minutes=age)
        write(
            at,
            {
                "action": "opened" if revision == 0 else "synchronize",
                "pull_request": {
                    "number": number,
                    "head": {"sha": sha, "ref": f"feedback-{number}"},
                    "created_at": at.isoformat(),
                    "updated_at": at.isoformat(),
                },
            },
        )
        return sha, at

    def run(number: int, sha: str, at: datetime, gate: int, *, attempt: int = 1, minute: int = 0) -> None:
        write(
            at + timedelta(minutes=minute),
            {
                "action": "requested",
                "workflow_run": {
                    "id": number * 10 + gate,
                    "name": WORKFLOWS[gate],
                    "run_attempt": attempt,
                    "head_sha": sha,
                    "head_branch": f"feedback-{number}",
                    "created_at": at.isoformat(),
                    "pull_requests": [{"number": number}],
                },
            },
            predicate="queued",
        )

    def job(number: int, sha: str, at: datetime, gate: int, minute: int, conclusion: str, *, attempt: int = 1) -> None:
        write(
            at + timedelta(minutes=minute),
            {
                "workflow_job": {
                    "id": number * 100 + gate * 10 + attempt,
                    "run_id": number * 10 + gate,
                    "run_attempt": attempt,
                    "head_sha": sha,
                    "name": CONTEXTS[gate],
                    "workflow_name": WORKFLOWS[gate],
                    "started_at": at.isoformat(),
                    "completed_at": (at + timedelta(minutes=minute)).isoformat(),
                    "conclusion": conclusion,
                }
            },
        )

    for number, age, conclusions in (
        (201, 160, ("success", "skipped", "neutral")),
        (202, 130, ("failure", "success", "success")),
        (203, 100, ("success", "success")),
        (204, 80, ("success", "success", "cancelled")),
        (205, 60, ("success", "success")),
        (206, 40, ("success", "success", "success")),
        (207, 30, ("success", "success", "success")),
        (209, 2, ()),
        (210, 25, ("success", "success", "new_upstream_conclusion")),
    ):
        sha, at = head(number, age)
        for gate in range(3):
            run(number, sha, at, gate)
        for gate, conclusion in enumerate(conclusions):
            job(number, sha, at, gate, gate + 2, conclusion)
        if number == 202:
            run(number, sha, at, 0, attempt=2, minute=10)
            job(number, sha, at, 0, 12, "success", attempt=2)
            # An out-of-order repeat from attempt one must not replace attempt two.
            job(number, sha, at, 0, 2, "failure")
        if number == 205:
            head(number, 50, revision=1)
        if number == 206:
            run(number, sha, at, 0, attempt=2, minute=3)
            job(number, sha, at, 0, 8, "success", attempt=2)
        if number == 207:
            run(number, sha, at, 0, attempt=2, minute=5)
            job(number, sha, at, 0, 6, "failure", attempt=2)

    # A -> B -> A are three occurrences, not one SHA cohort.
    old_sha, _ = head(208, 20)
    head(208, 18, revision=1)
    at = now - timedelta(minutes=16)
    write(
        at,
        {
            "action": "synchronize",
            "pull_request": {
                "number": 208,
                "head": {"sha": old_sha, "ref": "feedback-208"},
                "updated_at": at.isoformat(),
            },
        },
    )
    for gate in range(3):
        run(208, old_sha, at, gate)
        job(208, old_sha, at, gate, gate + 1, "success")

    # Unknown PR arrival is retained as a visibly different workflow-clock cohort.
    at = now - timedelta(minutes=12)
    sha = hashlib.sha256(b"211/0").hexdigest()[:40]
    for gate in range(3):
        run(211, sha, at, gate)
        job(211, sha, at, gate, gate + 1, "success")

    # A cancelled workflow overrides an unfinished job, but must not override a
    # job that had already finished when the cancellation arrived.
    sha, at = head(212, 15)
    for gate in range(2):
        run(212, sha, at, gate)
        if gate == 0:
            job(212, sha, at, gate, 1, "success")
        write(
            at + timedelta(minutes=2),
            {
                "action": "completed",
                "workflow_run": {
                    "id": 2120 + gate,
                    "name": WORKFLOWS[gate],
                    "run_attempt": 1,
                    "head_sha": sha,
                    "head_branch": "feedback-212",
                    "created_at": at.isoformat(),
                    "pull_requests": [{"number": 212}],
                    "conclusion": "cancelled",
                },
            },
        )


def assert_equivalent(connection: psycopg.Connection, now: datetime) -> None:
    # Compare complete row multisets at boundaries that cut through a rerun,
    # superseded head, repeated SHA, cancellation, and policy observation.
    for start, end in ((180, 0), (180, 10), (180, 19), (21, 15), (60, 30), (360, 300)):
        bounds = (now - timedelta(minutes=start), now - timedelta(minutes=end))
        difference = connection.execute(
            "(SELECT * FROM cdviz.ci_feedback(%s, %s) "
            "EXCEPT ALL SELECT * FROM cdviz.ci_feedback_baseline(%s, %s)) "
            "UNION ALL "
            "(SELECT * FROM cdviz.ci_feedback_baseline(%s, %s) "
            "EXCEPT ALL SELECT * FROM cdviz.ci_feedback(%s, %s))",
            bounds * 4,
        ).fetchall()
        assert difference == [], (bounds, difference)


def assert_histories(connection: psycopg.Connection, now: datetime) -> None:
    rows = (
        connection.cursor(row_factory=dict_row)
        .execute(
            "SELECT * FROM cdviz.ci_feedback(%s, %s) WHERE pr::integer BETWEEN 201 AND 211 ORDER BY arrived_at",
            (now - timedelta(hours=3), now),
        )
        .fetchall()
    )
    by_pr = {row["pr"]: row for row in rows}
    assert len(rows) == 14
    assert by_pr["201"]["green_seconds"] == 240  # skipped and neutral are accepted by GitHub.
    assert by_pr["202"]["red_seconds"] == 120
    assert by_pr["202"]["green_seconds"] == 720
    assert by_pr["202"]["first_verdict"] == "red"
    assert by_pr["202"]["state"] == "green"
    assert by_pr["203"]["first_green_at"] is None
    assert by_pr["203"]["state"] == "pending"
    assert by_pr["203"]["observed_gates"] == 3  # Includes the queued gate, with no result.
    assert by_pr["204"]["first_verdict"] is None
    assert by_pr["204"]["state"] == "cancelled"
    assert [r["state"] for r in rows if r["pr"] == "205"] == ["superseded", "pending"]
    assert by_pr["206"]["green_seconds"] == 480  # A queued rerun blocks stale green.
    assert by_pr["207"]["first_verdict"] == "green"
    assert by_pr["207"]["state"] == "red"
    assert by_pr["207"]["red_seconds"] == 360
    assert [r["state"] for r in rows if r["pr"] == "208"] == ["superseded", "superseded", "green"]
    assert by_pr["209"]["first_verdict"] is None
    assert by_pr["210"]["state"] == "unknown"
    assert by_pr["211"]["clock"] == "Workflow creation proxy"
    assert by_pr["211"]["green_seconds"] == 180
    cancellation = connection.execute(
        "SELECT state, observed_gates, missing_gates FROM cdviz.ci_feedback(%s, %s) WHERE pr = '212'",
        (now - timedelta(hours=3), now),
    ).fetchone()
    assert cancellation == ("cancelled", 2, "Build artifacts + imports check")


def assert_unknown_histories(connection: psycopg.Connection, now: datetime) -> None:
    for policy in ({"contexts": CONTEXTS, "supported": False}, {"contexts": [], "supported": True}):
        with connection.transaction(force_rollback=True):
            connection.execute(
                "INSERT INTO cdviz.cdevents_lake (timestamp, payload, subject, predicate, context_id) "
                "VALUES (%s, %s, 'change', 'updated', %s)",
                (now - timedelta(seconds=1), Jsonb({"customData": {"ci_policy": policy}}), str(uuid4())),
            )
            rows = connection.execute(
                "SELECT state, first_red_at, first_green_at FROM cdviz.ci_feedback(%s, %s)",
                (now - timedelta(hours=3), now),
            ).fetchall()
            assert rows
            assert all(row == ("unknown", None, None) for row in rows)
            assert_equivalent(connection, now)

    # GitHub source clocks have finite precision. Two different revisions at
    # the same instant cannot acquire an invented order from their SHA sort.
    with connection.transaction(force_rollback=True):
        original = connection.execute(
            "SELECT timestamp, payload FROM cdviz.cdevents_lake "
            "WHERE payload #>> '{customData,github,pull_request,number}' = '201' LIMIT 1"
        ).fetchone()
        assert original is not None
        at, payload = original
        payload["customData"]["github"]["pull_request"]["head"]["sha"] = "f" * 40
        connection.execute(
            "INSERT INTO cdviz.cdevents_lake (timestamp, payload, subject, predicate, context_id) "
            "VALUES (%s, %s, 'change', 'updated', %s)",
            (at, Jsonb(payload), str(uuid4())),
        )
        rows = connection.execute(
            "SELECT state, first_red_at, first_green_at FROM cdviz.ci_feedback(%s, %s) WHERE pr = '201'",
            (now - timedelta(hours=3), now),
        ).fetchall()
        assert rows == [("unknown", None, None), ("unknown", None, None)]
        assert_equivalent(connection, now)
