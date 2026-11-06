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
from third_party.containers import postgres_18_minimal, timescaledb_oss


@contextmanager
def timescaledb_postgres(
    tmp_path: Path, *, network: str | None = None, network_alias: str = "cdviz-db"
) -> Iterator[str]:
    """Yield an admin URL for a fresh PG18 server with the Timescale module preloaded.

    The Timescale extension is not created in the database. Consumers should run
    their normal migrations, which must create the extension and any hypertables.
    """
    suffix = re.sub(r"[^a-z0-9-]+", "-", tmp_path.name.lower()).strip("-")[:24] or "test"
    container_name = f"tsdb-{suffix}-{uuid.uuid4().hex[:8]}"
    extension_name = f"{container_name}-ext"
    extension_root = tmp_path / "timescaledb-extension"
    extension_root.mkdir()
    log_path = undeclared_outputs_dir() / f"{container_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    postgres_image = load_oci_image(postgres_18_minimal.LAYOUT_RLOCATION)
    extension_image = load_oci_image(timescaledb_oss.LAYOUT_RLOCATION)

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
            # Extract the extension payload from its pinned OCI image just as CNPG
            # makes it available to the PostgreSQL container through ImageVolume.
            # The extension image is a filesystem payload, without a command.
            # This container is never started; Docker still requires an entrypoint.
            run("docker", "create", "--name", extension_name, "--entrypoint", "/unused", extension_image)
            run("docker", "cp", f"{extension_name}:/share", str(extension_root))
            run("docker", "cp", f"{extension_name}:/lib", str(extension_root))

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
            run("docker", "exec", "--user", "0", container_name, "mkdir", "-p", "/extensions/timescaledb-oss")
            run("docker", "cp", str(extension_root / "share"), f"{container_name}:/extensions/timescaledb-oss/")
            run("docker", "cp", str(extension_root / "lib"), f"{container_name}:/extensions/timescaledb-oss/")
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
                "dynamic_library_path=/extensions/timescaledb-oss/lib",
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
            run("docker", "rm", "--force", extension_name, check=False)
