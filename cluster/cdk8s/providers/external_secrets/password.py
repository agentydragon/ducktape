"""Ergonomic wrapper for External Secrets Operator's `Password` generator, following
cdk8s-plus's own construction pattern: a class named after the kind, constructed as
`Password(scope, id, ...)`. Every keyword is a `PasswordSpec` field under its own name and type;
`None` leaves it unset, so ESO's own default applies. No ducktape password policy lives here --
that is `cluster.cdk8s.external_secrets.minted_secret.password_generator`'s own.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import (
    Password as _Password,
    PasswordSpec,
    PasswordSpecEncoding,
)


class Password(_Password):
    """ESO's `Password` generator. `length`, `no_upper` and `allow_repeat` are the fields the CRD
    itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        length: int,
        no_upper: bool,
        allow_repeat: bool,
        digits: int | None = None,
        symbols: int | None = None,
        symbol_characters: str | None = None,
        encoding: PasswordSpecEncoding | None = None,
        secret_keys: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=PasswordSpec(
                length=length,
                no_upper=no_upper,
                allow_repeat=allow_repeat,
                digits=digits,
                symbols=symbols,
                symbol_characters=symbol_characters,
                encoding=encoding,
                secret_keys=list(secret_keys) if secret_keys is not None else None,
            ),
        )
