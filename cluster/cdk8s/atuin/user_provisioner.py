"""The Job that creates the agentydragon user in Atuin's database, or updates its password."""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.atuin.server import DB_APP_SECRET, NAMESPACE
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "atuin-user-provisioner"
OUTPUT_DIR = f"{GENERATED_ROOT}/atuin/user-provisioner"
_SCRIPT_CONFIG_MAP = f"{NAME}-script"
_SCRIPT_DIR = "/scripts"
_SCRIPT = textwrap.dedent(
    '''\
    """Provision the agentydragon user in Atuin's PostgreSQL database.

    Idempotent: creates user if missing, updates password if the SOPS source
    changed, no-op if password already matches.
    """

    import os
    import secrets
    import sys

    import argon2
    import psycopg2

    USERNAME = "agentydragon"
    EMAIL = "agentydragon@allegedly.works"
    DB_HOST = "atuin-db-rw.atuin.svc.cluster.local"
    DB_PORT = 5432
    DB_NAME = "atuin"
    DB_USER = "atuin"


    def main() -> None:
        user_password = os.environ["ATUIN_USER_PASSWORD"]
        db_password = os.environ["POSTGRES_PASSWORD"]

        conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=db_password)
        conn.autocommit = True

        ph = argon2.PasswordHasher()

        with conn.cursor() as cur:
            cur.execute("SELECT id, password FROM users WHERE username = %s", (USERNAME,))
            row = cur.fetchone()

            if row is None:
                hashed = ph.hash(user_password)
                cur.execute(
                    "INSERT INTO users (username, email, password) VALUES (%s, %s, %s) RETURNING id",
                    (USERNAME, EMAIL, hashed),
                )
                user_id = cur.fetchone()[0]

                # Atuin's login endpoint requires an existing session row.
                token = secrets.token_urlsafe(24)
                cur.execute("INSERT INTO sessions (user_id, token) VALUES (%s, %s)", (user_id, token))
                print(f"Created user {USERNAME!r} (id={user_id}) with session.")
                return

            user_id, stored_hash = row

            try:
                ph.verify(stored_hash, user_password)
                print(f"User {USERNAME!r} exists and password matches. No-op.")
            except argon2.exceptions.VerifyMismatchError:
                new_hash = ph.hash(user_password)
                cur.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user_id))
                print(f"Updated password for user {USERNAME!r} (id={user_id}).")

        conn.close()


    if __name__ == "__main__":
        try:
            main()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    '''
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(
        chart,
        "script",
        metadata=k8s.ObjectMeta(name=_SCRIPT_CONFIG_MAP, namespace=NAMESPACE),
        data={"provision.py": _SCRIPT},
    )
    k8s.KubeJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(
            name=NAME, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}
        ),
        spec=k8s.JobSpec(
            # The script is idempotent (creates if missing, updates the password only when
            # the SOPS source changed, else no-op), so re-running is safe; the TTL lets a
            # failed run retry instead of holding this Kustomization unready forever.
            # Rationale in cluster/cdk8s/ha_mcp.py's HaMcpCredentialsProvisioner._add_job.
            ttl_seconds_after_finished=3600,
            backoff_limit=5,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": NAME}),
                spec=k8s.PodSpec(
                    restart_policy="OnFailure",
                    node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                    containers=[
                        k8s.Container(
                            name="provisioner",
                            image="python:3.14-alpine",
                            command=[
                                "sh",
                                "-c",
                                f"pip install --quiet psycopg2-binary argon2-cffi && python {_SCRIPT_DIR}/provision.py",
                            ],
                            env=[
                                k8s.EnvVar(
                                    name="ATUIN_USER_PASSWORD",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(
                                            name="atuin-user-password", key="user_password"
                                        )
                                    ),
                                ),
                                k8s.EnvVar(
                                    name="POSTGRES_PASSWORD",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name=DB_APP_SECRET, key="password")
                                    ),
                                ),
                            ],
                            volume_mounts=[k8s.VolumeMount(name="script", mount_path=_SCRIPT_DIR, read_only=True)],
                        )
                    ],
                    volumes=[k8s.Volume(name="script", config_map=k8s.ConfigMapVolumeSource(name=_SCRIPT_CONFIG_MAP))],
                ),
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def atuin_user_provisioner(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, atuin: Kustomization, user_agentydragon: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(atuin, user_agentydragon),
    )
