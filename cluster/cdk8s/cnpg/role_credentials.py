"""External Secrets credentials for CloudNativePG managed roles."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.cnpg.cluster import PostgresRef
from cluster.cdk8s.external_secrets.minted_secret import password_generator
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret


def mint_db_role_secret(
    scope: Construct,
    id: str,
    *,
    name: str,
    cluster: PostgresRef,
    role: str,
    database: str,
    url_key: str = "DATABASE_URL",
    secret_type: str | None = "kubernetes.io/basic-auth",
    target_annotations: dict[str, str] | None = None,
) -> None:
    """Mints `name` as a DB role credential: an ESO `Password` generator feeding a
    multi-field `ExternalSecret` template -- `username`/`password`/`host`/`port`/`dbname`,
    plus a `postgresql://...` connection string under `url_key`. The
    generator's own object is always `f"{name}-generator"`, distinct from the target
    Secret's name. `target_annotations` land on the generated Secret's own metadata, which always
    carries `cnpg.io/reload`: every caller names the Secret as a CloudNativePG managed role's
    `passwordSecret`, and without the label the operator applies a new password only at its
    next unrelated reconcile, while Reloader restarts the consumers at once.
    """
    service = cluster.rw
    generator = password_generator(
        scope, f"{id}-generator", name=f"{name}-generator", namespace=cluster.namespace, length=40, digits=8
    )
    ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=cluster.namespace),
        refresh_interval="8760h",
        data_from=[DataFrom.from_password_generator(generator)],
        template=ExternalSecretSpecTargetTemplate(
            type=secret_type,
            data={
                "username": role,
                "password": "{{ .password }}",
                "host": service.host,
                "port": str(service.port.number),
                "dbname": database,
                url_key: f"postgresql://{role}:{{{{ .password }}}}@{service.host}:{service.port.number}/{database}",
            },
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                labels={"cnpg.io/reload": "true"}, annotations=target_annotations
            ),
        ),
    )
