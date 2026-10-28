"""The Goose migrator serializes concurrent startup against a new database."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import pytest_bazel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/action_service/migrations/cmd/actions-migrate_/actions-migrate")
)
_GOOSE_VERSION = "action_service_goose_version"


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


@contextmanager
def _engine(url: str) -> Iterator[Engine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        env = {"AGENTPLANE_ACTIONS_DATABASE_URL": _wire_url(url)}
        runners = [
            subprocess.Popen([str(_MIGRATOR)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for runner in runners:
            stdout, stderr = runner.communicate(timeout=60)
            assert runner.returncode == 0, (stdout, stderr)
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 8 AND is_applied")) == 1
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND (table_name, column_name) IN ((:request, :action), (:event, :actor), (:decision, :evidence))"
                    ),
                    {
                        "request": "action_request",
                        "action": "action",
                        "event": "action_event",
                        "actor": "actor",
                        "decision": "action_decision",
                        "evidence": "policy_evidence",
                    },
                )
                == 0
            )


def test_populated_contract_requires_validation_and_preserves_rows(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    actor_proto = bytes.fromhex("0a1f0a0c6d6967726174696f6e2d6e73120f6d6967726174696f6e2d6163746f72")
    evidence_proto = bytes.fromhex("1a270a106d6967726174696f6e2d706f6c696379120d6d6967726174696f6e2d736574180120052801")
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        env = {"AGENTPLANE_ACTIONS_DATABASE_URL": _wire_url(url)}
        runner = subprocess.run([str(_MIGRATOR)], env=env, capture_output=True, text=True, check=False)
        assert runner.returncode == 0, (runner.stdout, runner.stderr)
        with engine.begin() as conn:
            # Recreate the expanded v7 shape so this fixture exercises the populated-database rollout gate.
            conn.execute(text("ALTER TABLE action_request ADD COLUMN action JSONB"))
            conn.execute(text("ALTER TABLE action_event ADD COLUMN actor JSONB"))
            conn.execute(text("ALTER TABLE action_decision ADD COLUMN policy_evidence JSONB"))
            conn.execute(text("ALTER TABLE action_request ALTER COLUMN action_group DROP NOT NULL"))
            conn.execute(text("ALTER TABLE action_request ALTER COLUMN action_name DROP NOT NULL"))
            for suffix, caller in (("1", "migration-caller"), ("2", "migration-caller-2")):
                conn.execute(
                    text(
                        "INSERT INTO action_request (id, idempotency_key, action, action_group, action_name, arguments, "
                        "state, version, created_at, updated_at, title, caller_namespace, caller_name) "
                        'VALUES (:id, :key, \'{"group":"typed-group","name":"typed-action"}\'::jsonb, '
                        "'typed-group', 'typed-action', CAST(:arguments AS jsonb), 'denied', 2, now(), now(), "
                        "'migration request', 'migration-ns', :caller)"
                    ),
                    {
                        "id": f"00000000-0000-0000-0000-00000000000{suffix}",
                        "key": f"contract-{suffix}",
                        "caller": caller,
                        "arguments": '{"input":{"count":3}}',
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO action_event (request_id, sequence, state, at, actor, actor_proto) VALUES "
                    "('00000000-0000-0000-0000-000000000001', 1, 'denied', now(), "
                    '\'{"account":{"namespace":"migration-ns","name":"migration-actor"}}\'::jsonb, '
                    "decode(:actor, 'hex')), "
                    "('00000000-0000-0000-0000-000000000002', 1, 'denied', now(), NULL, NULL)"
                ),
                {"actor": actor_proto.hex()},
            )
            conn.execute(
                text(
                    "INSERT INTO action_decision (id, request_id, verdict, provider, idempotency_key, decided_at, "
                    "policy_evidence, policy_evidence_proto) VALUES "
                    "('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'deny', "
                    "'migration-provider', 'decision-1', now(), "
                    "CAST(:legacy_evidence AS jsonb), decode(:evidence, 'hex')), "
                    "('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'deny', "
                    "'migration-provider', 'decision-2', now(), NULL, NULL)"
                ),
                {
                    "evidence": evidence_proto.hex(),
                    "legacy_evidence": (
                        '{"bindings":[],"policy_sets":[],"matched":{"namespace":"migration-policy",'
                        '"policy_set":"migration-set","source":"rules","index":5,"type":"exact_actions"}}'
                    ),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO action_execution (id, request_id, state, result, created_at) VALUES "
                    "('20000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', "
                    "'succeeded', '{\"kept\":[1,true,null]}'::jsonb, now())"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE action_request ADD CONSTRAINT action_request_typed_identity_present "
                    "CHECK (action_group IS NOT NULL AND action_name IS NOT NULL) NOT VALID"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE action_event ADD CONSTRAINT action_event_actor_proto_present "
                    "CHECK (actor IS NULL OR actor = 'null'::jsonb OR actor_proto IS NOT NULL) NOT VALID"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE action_decision ADD CONSTRAINT action_decision_policy_evidence_proto_present "
                    "CHECK (policy_evidence IS NULL OR policy_evidence = 'null'::jsonb OR policy_evidence_proto IS NOT NULL) NOT VALID"
                )
            )
            conn.execute(text(f"DELETE FROM {_GOOSE_VERSION} WHERE version_id = 8"))

        # The populated migration must refuse before changing any schema or advancing Goose.
        runner = subprocess.run([str(_MIGRATOR)], env=env, capture_output=True, text=True, check=False)
        assert runner.returncode != 0, (runner.stdout, runner.stderr)
        assert "run validate-storage --confirm-all-writers-typed-only" in runner.stderr
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 8 AND is_applied")) == 0
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND (table_name, column_name) IN ((:request, :action), (:event, :actor), (:decision, :evidence))"
                    ),
                    {
                        "request": "action_request",
                        "action": "action",
                        "event": "action_event",
                        "actor": "actor",
                        "decision": "action_decision",
                        "evidence": "policy_evidence",
                    },
                )
                == 3
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name = 'action_request' AND column_name IN ('action_group', 'action_name') "
                        "AND is_nullable = 'YES'"
                    )
                )
                == 2
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conname IN "
                        "('action_request_typed_identity_present', 'action_event_actor_proto_present', "
                        "'action_decision_policy_evidence_proto_present') AND NOT convalidated"
                    )
                )
                == 3
            )

        with engine.begin() as conn:
            for table, constraint in (
                ("action_request", "action_request_typed_identity_present"),
                ("action_event", "action_event_actor_proto_present"),
                ("action_decision", "action_decision_policy_evidence_proto_present"),
            ):
                conn.execute(text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"))

        runner = subprocess.run([str(_MIGRATOR)], env=env, capture_output=True, text=True, check=False)
        assert runner.returncode == 0, (runner.stdout, runner.stderr)
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 8 AND is_applied")) == 1
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND (table_name, column_name) IN ((:request, :action), (:event, :actor), (:decision, :evidence))"
                    ),
                    {
                        "request": "action_request",
                        "action": "action",
                        "event": "action_event",
                        "actor": "actor",
                        "decision": "action_decision",
                        "evidence": "policy_evidence",
                    },
                )
                == 0
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name = 'action_request' AND column_name IN ('action_group', 'action_name') "
                        "AND is_nullable = 'NO'"
                    )
                )
                == 2
            )
            assert conn.scalar(text("SELECT count(*) FROM action_request")) == 2
            assert conn.scalar(text("SELECT count(*) FROM action_event")) == 2
            assert conn.scalar(text("SELECT count(*) FROM action_decision")) == 2
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM action_request WHERE id = '00000000-0000-0000-0000-000000000001' "
                        "AND action_group = 'typed-group' AND action_name = 'typed-action' "
                        "AND arguments = CAST(:arguments AS jsonb)"
                    ),
                    {"arguments": '{"input":{"count":3}}'},
                )
                == 1
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM action_event WHERE request_id = '00000000-0000-0000-0000-000000000001' "
                        "AND actor_proto = decode(:actor, 'hex')"
                    ),
                    {"actor": actor_proto.hex()},
                )
                == 1
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM action_event WHERE request_id = '00000000-0000-0000-0000-000000000002' "
                        "AND actor_proto IS NULL"
                    )
                )
                == 1
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM action_decision WHERE request_id = '00000000-0000-0000-0000-000000000001' "
                        "AND policy_evidence_proto = decode(:evidence, 'hex')"
                    ),
                    {"evidence": evidence_proto.hex()},
                )
                == 1
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM action_decision WHERE request_id = '00000000-0000-0000-0000-000000000002' "
                        "AND policy_evidence_proto IS NULL"
                    )
                )
                == 1
            )
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM action_execution WHERE result = '{\"kept\":[1,true,null]}'::jsonb")
                )
                == 1
            )


if __name__ == "__main__":
    pytest_bazel.main()
