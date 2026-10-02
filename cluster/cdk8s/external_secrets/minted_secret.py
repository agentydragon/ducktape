"""ducktape mints its own bearer tokens and DB role credentials rather than storing
ciphertext for values nothing outside the cluster needs to know: an ESO `Password`
generator immediately consumed by a matching `ExternalSecret`. Two idioms share the
generator half: a single opaque secret (`mint_bearer_secret`) and a multi-field DB role
credential (`mint_db_role_secret`).
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret
from cluster.cdk8s.providers.external_secrets.password import Password


def password_generator(scope: Construct, id: str, *, name: str, namespace: str, length: int, digits: int) -> str:
    """Mints a `Password` generator named `name`, returning that name for
    `DataFrom.from_password_generator`. Our policy: no symbols, upper case and repeats allowed."""
    Password(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        length=length,
        digits=digits,
        symbols=0,
        no_upper=False,
        allow_repeat=True,
    )
    return name


def mint_bearer_secret(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    key: str | None = "password",
    length: int = 48,
    digits: int = 12,
    refresh: str | ExternalSecretSpecRefreshPolicy = ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
    creation_policy: ExternalSecretSpecTargetCreationPolicy | None = None,
    deletion_policy: ExternalSecretSpecTargetDeletionPolicy | None = None,
    immutable: bool | None = None,
    secret_type: str | None = "Opaque",
    generator_name: str | None = None,
    description: str | None = None,
    target_annotations: dict[str, str] | None = None,
) -> None:
    """Mints `name` as a single-key bearer/opaque secret: an ESO `Password` generator
    immediately consumed by a single-key `ExternalSecret` template.

    `key` names the field the generated password lands under in the target Secret;
    `key=None` omits the template, so ESO copies the generator's own `password` field into
    the target verbatim, under that same name. `generator_name` overrides the `Password`
    object's own name where it must differ from the target Secret's `name` -- both objects
    share one name at most call sites. `target_annotations` lands on the generated
    Secret's own metadata (e.g. reflector mirroring config); `description` is this
    `ExternalSecret` object's own `metadata.annotations.description`.
    """
    generator = password_generator(
        scope, f"{id}-generator", name=generator_name or name, namespace=namespace, length=length, digits=digits
    )
    template = (
        ExternalSecretSpecTargetTemplate(
            type=secret_type,
            data={key: "{{ .password }}"},
            metadata=ExternalSecretSpecTargetTemplateMetadata(annotations=target_annotations)
            if target_annotations
            else None,
        )
        if key is not None
        else None
    )
    ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(
            name=name, namespace=namespace, annotations={"description": description} if description else None
        ),
        refresh_interval=refresh if isinstance(refresh, str) else None,
        refresh_policy=refresh if isinstance(refresh, ExternalSecretSpecRefreshPolicy) else None,
        data_from=[DataFrom.from_password_generator(generator)],
        creation_policy=creation_policy,
        deletion_policy=deletion_policy,
        template=template,
        immutable=immutable,
    )


def mint_db_role_secret(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    role: str,
    host: str,
    port: int,
    database: str,
    url_scheme: str = "postgresql",
    url_key: str = "DATABASE_URL",
    include_host_fields: bool = True,
    secret_type: str | None = "kubernetes.io/basic-auth",
    target_annotations: dict[str, str] | None = None,
) -> None:
    """Mints `name` as a DB role credential: an ESO `Password` generator feeding a
    multi-field `ExternalSecret` template -- `username`/`password`, optionally `host`/
    `port`/`dbname`, plus a `{url_scheme}://...` connection string under `url_key`. The
    generator's own object is always `f"{name}-generator"`, distinct from the target
    Secret's name (unlike `mint_bearer_secret`, every DB role site already used this
    naming). `target_annotations` land on the generated Secret's own metadata, which always
    carries `cnpg.io/reload`: every caller names the Secret as a CloudNativePG managed role's
    `passwordSecret`, and without the label the operator applies a new password only at its
    next unrelated reconcile, while Reloader restarts the consumers at once.
    """
    generator = password_generator(
        scope, f"{id}-generator", name=f"{name}-generator", namespace=namespace, length=40, digits=8
    )
    data = {"username": role, "password": "{{ .password }}"}
    if include_host_fields:
        data |= {"host": host, "port": str(port), "dbname": database}
    data[url_key] = f"{url_scheme}://{role}:{{{{ .password }}}}@{host}:{port}/{database}"
    ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        refresh_interval="8760h",
        data_from=[DataFrom.from_password_generator(generator)],
        template=ExternalSecretSpecTargetTemplate(
            type=secret_type,
            data=data,
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                labels={"cnpg.io/reload": "true"}, annotations=target_annotations
            ),
        ),
    )
