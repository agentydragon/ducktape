"""ESO Password generators for cluster-owned secrets.

Each refresh produces a new password. Callers select CreatedOnce or a yearly refresh
according to the consumer's rotation policy. Credentials owned by another system,
such as Authentik OIDC client secrets, must be read from that owner.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret
from cluster.cdk8s.secret_ref import SecretKey, SecretRef


def password_generator(scope: Construct, id: str, *, name: str, namespace: str, length: int, digits: int) -> str:
    """Mints a `Password` generator named `name`, returning that name for
    `DataFrom.from_password_generator`. Our policy: no symbols, upper case and repeats allowed."""
    Password(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        spec=PasswordSpec(length=length, digits=digits, symbols=0, no_upper=False, allow_repeat=True),
    )
    return name


def mint_bearer_secret(
    scope: Construct,
    id: str,
    *,
    target: SecretKey | SecretRef,
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
    """Mint a single opaque credential. A SecretKey names its templated output field.
    A whole SecretRef receives the password generator's native output without a template.
    generator_name overrides the Password resource name independently of the Secret name.
    target_annotations apply to the templated Secret, and description to the ExternalSecret.
    """
    if isinstance(target, SecretKey):
        secret = target.secret
        template = ExternalSecretSpecTargetTemplate(
            type=secret_type,
            data={target.key: "{{ .password }}"},
            metadata=ExternalSecretSpecTargetTemplateMetadata(annotations=target_annotations)
            if target_annotations
            else None,
        )
    else:
        secret = target
        template = None

    generator = password_generator(
        scope,
        f"{id}-generator",
        name=generator_name or secret.name,
        namespace=secret.namespace,
        length=length,
        digits=digits,
    )
    ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(
            name=secret.name,
            namespace=secret.namespace,
            annotations={"description": description} if description else None,
        ),
        refresh_interval=refresh if isinstance(refresh, str) else None,
        refresh_policy=refresh if isinstance(refresh, ExternalSecretSpecRefreshPolicy) else None,
        data_from=[DataFrom.from_password_generator(generator)],
        creation_policy=creation_policy,
        deletion_policy=deletion_policy,
        template=template,
        immutable=immutable,
    )
