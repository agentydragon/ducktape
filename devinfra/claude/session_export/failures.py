"""One-line descriptions of failures, for the page and the logs."""

from collections.abc import Iterator

import httpx


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
    # `__notes__` exists only once `add_note` was called; the API client puts the error body there.
    return ": ".join([type(leaf).__name__, detail, *getattr(leaf, "__notes__", [])])


def describe_failure(failure: BaseException) -> str:
    return "; ".join(_describe_leaf(leaf) for leaf in _leaves(failure))
