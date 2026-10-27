"""A service's `Settings` is its deployment contract: each field is a
`--kebab-case` flag, an `<env_prefix>FIELD` environment variable (nested levels joined by
`env_nested_delimiter`; a `validation_alias` is the whole name, without the prefix) and a
key of the YAML settings file. Rendering a Deployment's flags, env var names and settings
file through the model fails at synth on a renamed or dropped field, instead of as a
CrashLoopBackOff on the cluster. Models for Go configuration can carry this authoring
metadata without loading any settings in Python.
"""

from __future__ import annotations

import types
from typing import Annotated, Any, TypeAliasType, Union, cast, get_args, get_origin

from more_itertools import one
from pydantic import BaseModel, Field, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import SettingsConfigDict


def _resolve(annotation: Any) -> tuple[Any, str | None]:
    """The type behind PEP 695 aliases and `Annotated` metadata, with the discriminator a
    `Field(discriminator=...)` on the way named."""
    discriminator = None
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation, *metadata = get_args(annotation)
            for info in metadata:
                if isinstance(info, FieldInfo) and isinstance(info.discriminator, str):
                    discriminator = info.discriminator
        else:
            return annotation, discriminator


def _without_none(annotation: Any) -> Any:
    if get_origin(annotation) in (types.UnionType, Union):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        return members[0] if len(members) == 1 else Union[tuple(members)]  # noqa: UP007
    return annotation


def _members(annotation: Any) -> list[Any]:
    return list(get_args(annotation)) if get_origin(annotation) in (types.UnionType, Union) else [annotation]


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _field_annotation(model: Any, name: str) -> Any:
    """`name`'s annotation on `model`, or the one annotation the members of a union that have
    the field agree on. Pydantic moves a field's own `Annotated[..., Field(discriminator=...)]`
    onto its `FieldInfo`; the discriminator goes back on the annotation for `_resolve`."""
    model, _ = _resolve(model)
    members = [member for member in _members(_without_none(model)) if _is_model(member)]
    if not members:
        raise TypeError(f"{model!r} is not a model, so it has no field {name!r}")
    fields = [member.model_fields[name] for member in members if name in member.model_fields]
    if not fields:
        raise KeyError(f"{' | '.join(member.__name__ for member in members)} has no field {name!r}")
    first = fields[0]
    if any((field.annotation, field.discriminator) != (first.annotation, first.discriminator) for field in fields):
        raise TypeError(f"field {name!r} is typed differently across {model!r}")
    if isinstance(first.discriminator, str):
        return Annotated[first.annotation, Field(discriminator=first.discriminator)]
    return first.annotation


def env_name(settings: type[BaseModel], field: str, *path: str) -> str:
    """The environment variable for `field`, or for a field nested under it: each `path`
    segment is a field of the nested model, except that a dict-valued field's key is taken
    verbatim (`env_name(Settings, "mcp_servers", "github", "client_id")`)."""
    annotation = _field_annotation(settings, field)
    for segment in path:
        annotation = _without_none(_resolve(annotation)[0])
        if get_origin(annotation) is dict:
            annotation = get_args(annotation)[1]
        else:
            annotation = _field_annotation(annotation, segment)
    config = cast(SettingsConfigDict, settings.model_config)
    delimiter = config.get("env_nested_delimiter")
    if path and not delimiter:
        raise ValueError(f"{settings.__name__} nests no settings in environment variables")
    alias = settings.model_fields[field].validation_alias
    if alias is None:
        head = f"{config.get('env_prefix', '')}{field}"
    elif isinstance(alias, str):
        head = alias
    else:
        raise TypeError(f"{settings.__name__}.{field}'s alias {alias!r} is not one environment variable")
    return (delimiter or "").join([head, *path]).upper()


def cli_args(settings: type[BaseModel], **values: object) -> list[str]:
    """`--kebab-case=value` flags, each value checked against the field it sets."""
    if not settings.model_config.get("cli_kebab_case"):
        raise ValueError(f"{settings.__name__} does not take kebab-case flags")
    for name, value in values.items():
        TypeAdapter(_field_annotation(settings, name)).validate_python(value)
    return [f"--{name.replace('_', '-')}={value}" for name, value in values.items()]


def checked_value[T](settings: type[BaseModel], field: str, value: T) -> T:
    """`value` once it validates as `field`'s whole type -- for a field a Deployment supplies
    serialized in one piece (a JSON ConfigMap key, an env var)."""
    TypeAdapter(_field_annotation(settings, field)).validate_python(value)
    return value


def _discriminated(members: list[Any], discriminator: str | None, value: dict[str, Any]) -> Any:
    """The member of a discriminated union `value` is an instance of, by its tag."""
    if discriminator is None:
        raise TypeError(f"{members!r} has no discriminator to pick a member of by")
    tag = value[discriminator]
    return one(
        member for member in members if tag in get_args(_resolve(member.model_fields[discriminator].annotation)[0])
    )


def _check_present(annotation: Any, value: Any) -> None:
    """Check present values against their field annotations, recursing through model mappings.

    Model mappings are checked field by field, so a fragment may omit required nested fields.
    Completeness and cross-field rules remain the responsibility of full settings validation. This
    does not normalize values.
    """
    resolved, discriminator = _resolve(annotation)
    resolved = _without_none(resolved)
    members = _members(resolved)
    if isinstance(value, dict) and all(_is_model(member) for member in members):
        model = _discriminated(members, discriminator, value) if len(members) > 1 else members[0]
        for name, item in value.items():
            _check_present(_field_annotation(model, name), item)
    elif get_origin(resolved) is dict and isinstance(value, dict):
        key_type, value_type = get_args(resolved)
        for key, item in value.items():
            TypeAdapter(key_type).validate_python(key)
            _check_present(value_type, item)
    else:
        TypeAdapter(annotation).validate_python(value)


def validate_settings_fragment(model: type[BaseModel], fragment: dict[str, Any]) -> None:
    """Check each present fragment key and value against its model field annotation.

    This checks field names and present values against their annotations without normalizing them.
    Model mappings are checked field by field, so completeness and cross-field rules remain the
    responsibility of the settings source that combines file and environment values and validates
    the effective settings.
    """
    for name, value in fragment.items():
        _check_present(_field_annotation(model, name), value)
