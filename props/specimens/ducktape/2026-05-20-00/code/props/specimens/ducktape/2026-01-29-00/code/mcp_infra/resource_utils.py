from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastmcp.client.client import Client
from fastmcp.server.server import add_resource_prefix as _fastmcp_add_resource_prefix, has_resource_prefix
from pydantic import TypeAdapter
from pydantic.networks import AnyUrl

from mcp_utils.resources import extract_single_text_content


def add_resource_prefix(uri: str | AnyUrl, prefix: str) -> str:
    """Add a prefix to a resource URI.

    Wrapper around FastMCP's add_resource_prefix that accepts both str and AnyUrl.
    FastMCP resources expose .uri as AnyUrl, but add_resource_prefix expects str.
    """
    uri_str = str(uri) if isinstance(uri, AnyUrl) else uri
    return _fastmcp_add_resource_prefix(uri_str, prefix)


async def read_text(client: Client[Any], uri: AnyUrl | str) -> str:
    """Read a text resource and return its content.

    Args:
        client: FastMCP client instance
        uri: Resource URI (AnyUrl or string)

    Returns:
        Text content of the resource

    Raises:
        RuntimeError: If resource doesn't contain exactly one text part
    """
    uri_obj = AnyUrl(uri) if isinstance(uri, str) else uri
    contents = await client.read_resource(uri_obj)
    return extract_single_text_content(contents)


async def read_text_json_typed[T](client: Client[Any], uri: AnyUrl | str, model: type[T] | Any) -> T:
    """Read a text JSON resource and parse it as the given Pydantic model/type.

    Args:
        client: FastMCP client instance
        uri: Resource URI (AnyUrl or string)
        model: Type (class, Union, Annotated, etc.) that TypeAdapter can handle

    Returns:
        Parsed model instance

    - Validates exactly one text part
    - Parses JSON into the provided model/type using TypeAdapter(model).validate_json
    - Accepts concrete types (type[T]) and type expressions (Union, Annotated, etc.)
    - Type inference works for concrete types; Union types require explicit annotation
    """
    # Convert str to AnyUrl if needed
    uri_obj: AnyUrl = AnyUrl(uri) if isinstance(uri, str) else uri
    contents = await client.read_resource(uri_obj)
    validated: T = TypeAdapter(model).validate_json(extract_single_text_content(contents))
    return validated


def derive_origin_server(uri: str, mount_names: Iterable[str]) -> str:
    """Find which mounted server owns the given resource URI.

    Uses FastMCP's path format (protocol://prefix/path).
    Raises ValueError if no server matches.
    """
    sorted_names = sorted(mount_names)
    for name in sorted_names:
        if has_resource_prefix(uri, name):
            return name

    raise ValueError(f"Could not derive origin server for URI {uri!r}. Available servers: {sorted_names}")
