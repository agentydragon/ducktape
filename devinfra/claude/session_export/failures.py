"""Failures of the API clients: the error body attached at the raise, and one-line descriptions for the page and the logs."""

from collections.abc import Iterator

import httpx

_ERROR_BODY_CHARS = 300


def raise_for_status_with_body(response: httpx.Response) -> None:
    """`response.raise_for_status()`, with the start of the body attached as a note.

    The body says why (a missing header, `invalid_grant`, the API's own error type); `describe_failure` appends it.
    A streamed response must be read first.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        e.add_note(response.text[:_ERROR_BODY_CHARS])
        raise


def _leaves(failure: BaseException) -> Iterator[BaseException]:
    if isinstance(failure, BaseExceptionGroup):
        for member in failure.exceptions:
            yield from _leaves(member)
    else:
        yield failure


def _describe_leaf(leaf: BaseException) -> str:
    if isinstance(leaf, httpx.HTTPStatusError):
        # Not `str(leaf)`: that carries the whole URL, query string included, and a help link.
        detail = f"{leaf.response.status_code} {leaf.response.reason_phrase} from {leaf.request.method} {leaf.request.url.path}"
    else:
        detail = str(leaf)
    # `__notes__` exists only once `add_note` was called; `raise_for_status_with_body` puts the error body there.
    return ": ".join([type(leaf).__name__, detail, *getattr(leaf, "__notes__", [])])


def describe_failure(failure: BaseException) -> str:
    return "; ".join(_describe_leaf(leaf) for leaf in _leaves(failure))
