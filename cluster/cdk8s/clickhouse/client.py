"""The cluster's central ClickHouse as its clients address it, and the
`clickhouse-client --queries-file` container that applies a ConfigMap-mounted SQL file
over its native port: the database-bootstrap Job and aiquota's `migrate` init container
differ only in the schema they mount and the credentials they present.

Port 9000, not 9440: the Altinity operator generates 9440 secure remote-server entries
when `secure: true` is set, but ClickHouse has no TLS listener unless one is configured
separately, so a 9440 remote entry cannot be recognized as local by DDLWorker and
`ON CLUSTER` DDL never completes.
"""

from __future__ import annotations

from cdk8s import Size
from cdk8s_plus_34 import (
    Capability,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    EnvValue,
    IConfigMap,
    ImagePullPolicy,
    ISecret,
    MemoryResources,
    SecretValue,
    Volume,
    VolumeMount,
)
from constructs import Construct

HOST = "clickhouse.clickhouse.svc.cluster.local"
NATIVE_PORT = 9000
HTTP_PORT = 8123
SCHEMA_FILE = "schema.sql"  # the key of every schema ConfigMap, and the hand-written file it is generated from

_IMAGE = (
    "clickhouse/clickhouse-server:26.8.3.105@sha256:d73903d1b61dfe825fc3810542f252966f33d3fd8efb3b3edcbbafb46b524b04"
)
_SCHEMA_DIR = "/schema"
_TMP_DIR = "/tmp"


def queries_file_container(scope: Construct, name: str, *, schema: IConfigMap, credentials: ISecret) -> ContainerProps:
    """Runs `schema`'s `SCHEMA_FILE` key as the user in `credentials` (`username`/`password` keys)."""
    return ContainerProps(
        name=name,
        image=_IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        command=["clickhouse-client"],
        args=[
            f"--host={HOST}",
            f"--port={NATIVE_PORT}",
            "--connect_timeout=10",
            "--receive_timeout=60",
            "--multiquery",
            f"--queries-file={_SCHEMA_DIR}/{SCHEMA_FILE}",
        ],
        env_variables={
            "CLICKHOUSE_USER": EnvValue.from_secret_value(SecretValue(secret=credentials, key="username")),
            "CLICKHOUSE_PASSWORD": EnvValue.from_secret_value(SecretValue(secret=credentials, key="password")),
            # clickhouse-client writes its history under $HOME; the root filesystem is read-only.
            "HOME": EnvValue.from_value(_TMP_DIR),
        },
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(250)),
            memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
        ),
        security_context=ContainerSecurityContextProps(
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), read_only_root_filesystem=True
        ),
        volume_mounts=[
            VolumeMount(
                path=_SCHEMA_DIR,
                volume=Volume.from_config_map(scope, f"{name}-schema-volume", schema, name="schema"),
                read_only=True,
            ),
            VolumeMount(path=_TMP_DIR, volume=Volume.from_empty_dir(scope, f"{name}-tmp-volume", "tmp")),
        ],
    )
