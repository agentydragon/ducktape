"""Docker test fixture for CNPG's pinned PostgreSQL 18 + TimescaleDB extension."""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from third_party.containers import timescaledb_postgres_18


@contextmanager
def timescaledb_postgres(
    tmp_path: Path, *, network: str | None = None, network_alias: str = "cdviz-db"
) -> Iterator[str]:
    """Yield an admin URL for a fresh PG18 server with the Timescale module preloaded.

    The Timescale extension is not created in the database. Create it with the
    admin connection before running migrations as an unprivileged application role.
    """
    suffix = re.sub(r"[^a-z0-9-]+", "-", tmp_path.name.lower()).strip("-")[:24] or "test"
    container_name = f"tsdb-{suffix}-{uuid.uuid4().hex[:8]}"
    log_path = undeclared_outputs_dir() / f"{container_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    postgres_image = load_oci_image(timescaledb_postgres_18.LAYOUT_RLOCATION)

    with log_path.open("w") as log:

        def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            log.write("$ " + " ".join(args) + "\n")
            log.flush()
            result = subprocess.run(args, text=True, capture_output=True, check=False)
            if result.stdout:
                log.write(result.stdout)
            if result.stderr:
                log.write(result.stderr)
            log.flush()
            if check and result.returncode:
                raise AssertionError(f"Command failed ({result.returncode}); see {log_path}")
            return result

        def capture_postgres_logs() -> None:
            result = run("docker", "logs", container_name, check=False)
            if result.returncode:
                log.write(f"Could not read PostgreSQL container logs (exit {result.returncode}).\n")
                log.flush()

        try:
            create_args = ["docker", "create", "--name", container_name, "--publish", "127.0.0.1::5432"]
            if network is not None:
                create_args.extend(["--network", network, "--network-alias", network_alias])
            create_args.extend(
                [
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,nodev,size=256m,uid=26,gid=26",
                    "--entrypoint",
                    "/bin/sh",
                    postgres_image,
                    "-c",
                    "sleep infinity",
                ]
            )
            run(*create_args)
            run("docker", "start", container_name)
            run(
                "docker",
                "exec",
                container_name,
                "initdb",
                "-D",
                "/var/lib/postgresql/data",
                "--username=postgres",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=trust",
            )
            run(
                "docker",
                "exec",
                container_name,
                "/bin/sh",
                "-c",
                "printf '%s\\n' 'host all postgres all trust' 'host all all all scram-sha-256' "
                ">> /var/lib/postgresql/data/pg_hba.conf",
            )
            run(
                "docker",
                "exec",
                "--detach",
                container_name,
                "postgres",
                "-D",
                "/var/lib/postgresql/data",
                "-c",
                "listen_addresses=*",
                "-c",
                "unix_socket_directories=/tmp",
                "-c",
                "shared_preload_libraries=timescaledb",
                "-c",
                "extension_control_path=/extensions/timescaledb-oss/share",
                "-c",
                "dynamic_library_path=/extensions/timescaledb-oss/lib:$libdir",
                "-c",
                "timescaledb.telemetry_level=off",
                "-c",
                "max_locks_per_transaction=128",
            )

            for _ in range(60):
                if (
                    run(
                        "docker", "exec", container_name, "pg_isready", "-h", "/tmp", "-U", "postgres", check=False
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(1)
            else:
                raise AssertionError(f"PostgreSQL did not become ready; see {log_path}")

            port_line = run("docker", "port", container_name, "5432/tcp").stdout.strip().splitlines()[0]
            host_port = port_line.rsplit(":", maxsplit=1)[1]
            yield f"postgresql://postgres@127.0.0.1:{host_port}/postgres"
        except Exception:
            capture_postgres_logs()
            raise
        finally:
            run("docker", "rm", "--force", container_name, check=False)
