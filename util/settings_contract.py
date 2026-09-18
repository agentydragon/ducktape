"""A service's `Settings` is its deployment contract: each field is a
`--kebab-case` flag, an `<env_prefix>FIELD` environment variable (nested levels joined by
`env_nested_delimiter`) and a key of the YAML settings file. Rendering a Deployment's flags,
env var names and settings file through the model fails at synth on a renamed or dropped
field, instead of as a CrashLoopBackOff on the cluster.
"""

from __future__ import annotations

import types
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter
from pydantic_settings import BaseSettings


def _field_annotation(model: Any, name: str) -> Any:
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise TypeError(f"{model!r} is not a model, so it has no field {name!r}")
    if name not in model.model_fields:
        raise KeyError(f"{model.__name__} has no field {name!r}")
    return model.model_fields[name].annotation


def _without_none(annotation: Any) -> Any:
    if get_origin(annotation) in (types.UnionType, Union):
        (annotation,) = (arg for arg in get_args(annotation) if arg is not type(None))
    return annotation


def env_name(settings: type[BaseSettings], field: str, *path: str) -> str:
    """The environment variable for `field`, or for a field nested under it: each `path`
    segment is a field of the nested model, except that a dict-valued field's key is taken
    verbatim (`env_name(Settings, "mcp_servers", "github", "client_id")`)."""
    annotation = _field_annotation(settings, field)
    segments = [field]
    for segment in path:
        annotation = _without_none(annotation)
        if get_origin(annotation) is dict:
            annotation = get_args(annotation)[1]
        else:
            annotation = _field_annotation(annotation, segment)
        segments.append(segment)
    config = settings.model_config
    delimiter = config.get("env_nested_delimiter")
    if len(segments) > 1 and not delimiter:
        raise ValueError(f"{settings.__name__} nests no settings in environment variables")
    return f"{config.get('env_prefix', '')}{(delimiter or '').join(segments)}".upper()


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


def _check_present(annotation: Any, value: Any) -> None:
    """Validate what `value` carries of `annotation`. A nested model's remaining fields may come
    from another source (a secret's env var completes `web_push`), so a mapping is checked key by
    key against the model's fields rather than as a whole."""
    annotation = _without_none(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
        for name, item in value.items():
            _check_present(_field_annotation(annotation, name), item)
    elif get_origin(annotation) is dict and isinstance(value, dict):
        key_type, value_type = get_args(annotation)
        for key, item in value.items():
            TypeAdapter(key_type).validate_python(key)
            _check_present(value_type, item)
    else:
        TypeAdapter(annotation).validate_python(value)


def settings_file(settings: type[BaseSettings], content: dict[str, Any]) -> dict[str, Any]:
    """`content` as the settings file's data, each key checked against the field it sets."""
    for name, value in content.items():
        _check_present(_field_annotation(settings, name), value)
    return content
