"""Run the production Notification service against a caller-owned API and database."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import grpc
from python.runfiles import Runfiles

from agentplane.net.peertls.config import NOTIFICATIONS_URI
from agentplane.notification_service.api.notifications import notifications_pb2, notifications_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.db_url import render_postgresql_url

# gazelle:include_dep @pypi//protobuf

_SERVICE_BINARY = "ducktape/agentplane/notification_service/notification-service_/notification-service"
_DRIVER_BINARY = (
    "ducktape/agentplane/notification_service/testing/process_fixture/process_contract_test_/process_contract_test"
)
_MIGRATION_DATABASE_URL_ENV = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
_PROCESS_CONFIG_ENV = "DUCKTAPE_NOTIFICATION_PROCESS_CONFIG"
_TOKEN_AUDIENCE = "agentplane-notifications"
_STOP_GRACE_SECONDS = 10
_DUMP_SECONDS = 5
_STARTUP_SECONDS = 30


@dataclass(frozen=True)
class NotificationProcess:
    """A live service process and the real subscription row seeded for its Sandbox."""

    process: asyncio.subprocess.Process
    address: str
    subscription_id: UUID
    idempotency_key: str
    request_id: UUID
    stderr_path: Path


@asynccontextmanager
async def notification_process(
    *, database_url: str, api: KubernetesApiServer, sandbox: SandboxCluster, ca: CertificateAuthority, directory: Path
) -> AsyncIterator[NotificationProcess]:
    """Serve Notifications using the supplied API, CA and migrated caller-owned database.

    The production binary owns migration; the in-package test driver seeds the owned row through
    `store.Subscribe` and calls the production `serve` function with the supplied kubeconfig.
    """
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    runfiles = Runfiles.CreateOrRaise().root()
    service_binary = runfiles / _SERVICE_BINARY
    driver_binary = runfiles / _DRIVER_BINARY
    missing = [str(path) for path in (service_binary, driver_binary) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Notification process runfiles are missing: {', '.join(missing)}")

    go_database_url = render_postgresql_url(database_url, "postgresql", query={"sslmode": "disable"})
    migration_log = undeclared_outputs_dir() / f"notification-migrate-{uuid4().hex}.log"
    await asyncio.to_thread(_migrate, service_binary, go_database_url, migration_log)

    work = directory / f"notification-process-{uuid4().hex}"
    work.mkdir()
    server_cert, server_key = issue_workload_leaf(ca, NOTIFICATIONS_URI, work, dns_name="localhost")
    caller_directory = work / "caller"
    caller_directory.mkdir()
    client_cert, client_key = issue_workload_leaf(
        ca, sandbox.environment_runtime_identity_uri, caller_directory, dns_name="localhost"
    )
    server_key.chmod(0o600)
    client_key.chmod(0o600)
    client_ca = work / "client-ca.pem"
    client_ca.write_bytes(ca.cert_pem)
    peer_token = work / "unused-peer-token"
    peer_token.write_text("test-only unused peer token\n", encoding="utf-8")
    peer_token.chmod(0o600)

    config_path = work / "process.json"
    config_path.write_text(
        json.dumps(
            {
                "database_url": go_database_url,
                "kubeconfig_path": str(api.kubeconfig),
                "namespace": sandbox.namespace,
                "token_audience": _TOKEN_AUDIENCE,
                "server_cert_file": str(server_cert),
                "server_key_file": str(server_key),
                "client_ca_file": str(client_ca),
                "peer_token_file": str(peer_token),
                "seed_subscription": {
                    "sandbox_name": sandbox.name,
                    "sandbox_uid": sandbox.uid,
                    "identity_name": sandbox.execution_identity_name,
                    "identity_uid": sandbox.execution_identity_uid,
                },
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    stderr_path = undeclared_outputs_dir() / f"notification-process-{uuid4().hex}.stderr"
    process_env = os.environ.copy()
    process_env[_PROCESS_CONFIG_ENV] = str(config_path)
    process_env["OTEL_SDK_DISABLED"] = "true"
    # The Python test owns JUnit and the child lifetime. rules_go's XML wrapper
    # otherwise re-execs the driver and ignores TERM on the PID we own.
    process_env["GO_TEST_WRAP"] = "0"
    with stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            str(driver_binary),
            "-test.run=^TestNotificationProcessDriver$",
            env=process_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )

    # Cleanup is active immediately after Start, including readiness and parsing failures.
    primary_error: BaseException | None = None
    try:
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=_STARTUP_SECONDS)
        except TimeoutError as error:
            raise RuntimeError(
                f"Notification process did not report readiness; stderr artifact: {stderr_path}"
            ) from error
        if not line:
            return_code = await process.wait()
            raise RuntimeError(
                f"Notification process exited before readiness ({return_code}); stderr artifact: {stderr_path}"
            )
        try:
            ready = json.loads(line)
            address = _localhost_address(ready["grpc_address"])
            seeded = ready["subscription"]
            subscription_id = UUID(seeded["id"])
            idempotency_key = str(seeded["idempotency_key"])
            request_id = UUID(seeded["request_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Notification process returned invalid readiness metadata; stderr artifact: {stderr_path}"
            ) from error
        service = NotificationProcess(
            process=process,
            address=address,
            subscription_id=subscription_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            stderr_path=stderr_path,
        )
        await _wait_until_seed_is_readable(service, ca, client_cert, client_key)
        yield service
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            await _stop(process, stderr_path)
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"Notification process cleanup also failed: {cleanup_error}")


def _migrate(service_binary: Path, database_url: str, log_path: Path) -> None:
    env = os.environ.copy()
    env[_MIGRATION_DATABASE_URL_ENV] = database_url
    try:
        with log_path.open("wb") as log:
            result = subprocess.run(
                [str(service_binary), "--migrate-only"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Notification database migration timed out; log artifact: {log_path}") from error
    if result.returncode != 0:
        raise RuntimeError(f"Notification database migration failed with {result.returncode}; log artifact: {log_path}")


def _localhost_address(address: str) -> str:
    host, separator, port = address.rpartition(":")
    if not separator or host != "127.0.0.1" or not port.isdigit():
        raise ValueError(f"unexpected Notification listener address {address!r}")
    return f"localhost:{port}"


async def _wait_until_seed_is_readable(
    process: NotificationProcess, ca: CertificateAuthority, client_cert: Path, client_key: Path
) -> None:
    private_key, certificate_chain = await asyncio.gather(
        asyncio.to_thread(client_key.read_bytes), asyncio.to_thread(client_cert.read_bytes)
    )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.cert_pem, private_key=private_key, certificate_chain=certificate_chain
    )
    channel = grpc.aio.secure_channel(process.address, credentials)
    try:
        stub = notifications_pb2_grpc.NotificationsStub(channel)
        deadline = asyncio.get_running_loop().time() + _STARTUP_SECONDS
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            if process.process.returncode is not None:
                raise RuntimeError(
                    f"Notification process exited before its seeded row was readable "
                    f"({process.process.returncode}); stderr artifact: {process.stderr_path}"
                )
            try:
                response = await stub.ListSubscriptions(notifications_pb2.ListSubscriptionsRequest(), timeout=1)
                matches = [item for item in response.subscriptions if item.id == str(process.subscription_id)]
                if len(matches) == 1:
                    row = matches[0]
                    if row.idempotency_key != process.idempotency_key or row.source.actions.request_id != str(
                        process.request_id
                    ):
                        raise RuntimeError(
                            "Notification process returned a seeded row with different owner data; "
                            f"stderr artifact: {process.stderr_path}"
                        )
                    return
                last_error = RuntimeError(
                    f"ListSubscriptions returned {len(response.subscriptions)} rows and no seeded row"
                )
            except grpc.aio.AioRpcError as error:
                last_error = error
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"Notification process did not return its seeded row before the startup deadline "
            f"({last_error}); stderr artifact: {process.stderr_path}"
        )
    finally:
        await channel.close()


async def _stop(process: asyncio.subprocess.Process, stderr_path: Path) -> None:
    signal_error: OSError | None = None
    if process.returncode is None:
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            signal_error = error
        try:
            # The test driver can still write its final result after readiness.
            # Drain its pipe while waiting rather than leave a full pipe holding
            # the child or asyncio's transport teardown open.
            await asyncio.wait_for(process.communicate(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            kill_error: OSError | None = None
            try:
                # Go's standard QUIT handler records the blocked goroutines in
                # the existing stderr artifact before it exits.
                process.send_signal(signal.SIGQUIT)
            except ProcessLookupError:
                pass
            except OSError as error:
                kill_error = error
            try:
                await asyncio.wait_for(process.communicate(), timeout=_DUMP_SECONDS)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    kill_error = error
                await process.communicate()
            raise RuntimeError(
                f"Notification process did not stop after SIGTERM; stderr artifact: {stderr_path}"
            ) from kill_error
    if signal_error is not None:
        raise RuntimeError(
            f"could not signal Notification process to stop; stderr artifact: {stderr_path}"
        ) from signal_error
    if process.returncode != 0:
        raise RuntimeError(f"Notification process exited with {process.returncode}; stderr artifact: {stderr_path}")
