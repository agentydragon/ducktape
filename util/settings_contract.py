"""A service's `Settings` is its deployment contract: each field is a
`--kebab-case` flag, an `<env_prefix>FIELD` environment variable (nested levels joined by
`env_nested_delimiter`; a `validation_alias` is the whole name, without the prefix) and a
key of the YAML settings file. Rendering a Deployment's flags, env var names and settings
file through the model fails at synth on a renamed or dropped field, instead of as a
CrashLoopBackOff on the cluster.
"""

from __future__ import annotations

import copy
import types
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, TypeAliasType, Union, get_args, get_origin

from more_itertools import one
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings

# Stands in for a leaf another source supplies when the whole file validates as its model.
_SUPPLIED = "supplied-by-another-source"


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
    the field agree on."""
    model, _ = _resolve(model)
    members = [member for member in _members(_without_none(model)) if _is_model(member)]
    if not members:
        raise TypeError(f"{model!r} is not a model, so it has no field {name!r}")
    annotations = [member.model_fields[name].annotation for member in members if name in member.model_fields]
    if not annotations:
        raise KeyError(f"{' | '.join(member.__name__ for member in members)} has no field {name!r}")
    if any(annotation != annotations[0] for annotation in annotations):
        raise TypeError(f"field {name!r} is typed differently across {model!r}")
    return annotations[0]


def env_name(settings: type[BaseSettings], field: str, *path: str) -> str:
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
    config = settings.model_config
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


def cli_args(settings: type[BaseSettings], **values: object) -> list[str]:
    """`--kebab-case=value` flags, each value checked against the field it sets."""
    if not settings.model_config.get("cli_kebab_case"):
        raise ValueError(f"{settings.__name__} does not take kebab-case flags")
    for name, value in values.items():
        TypeAdapter(_field_annotation(settings, name)).validate_python(value)
    return [f"--{name.replace('_', '-')}={value}" for name, value in values.items()]


def checked_value[T](settings: type[BaseSettings], field: str, value: T) -> T:
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
    """Validate what `value` carries of `annotation`. A nested model's remaining fields may come
    from another source (a secret's env var completes `web_push`), so a mapping is checked key by
    key against the model's fields rather than as a whole."""
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


def settings_file(
    model: type[BaseModel], content: dict[str, Any], *, supplied: Iterable[Sequence[str]] = ()
) -> dict[str, Any]:
    """`content` as the settings file's data, each key checked against the field it sets.

    `supplied` names the paths of leaves another source completes (a Secret's env var); with
    it the whole file also validates as `model` -- its cross-field rules included -- with a
    placeholder string standing in for each of those leaves.
    """
    for name, value in content.items():
        _check_present(_field_annotation(model, name), value)
    supplied = list(supplied)
    if supplied:
        completed = copy.deepcopy(content)
        for path in supplied:
            node = completed
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = _SUPPLIED
        model.model_validate(completed)
    return content
