from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable

from aiquota.models import HistoryObservation, ProviderFetch


class Provider(ABC):
    """One AI subscription whose quota status we can poll.

    Subclasses are constructed with their typed settings from `Config` and
    expose a single `fetch()` method returning the latest quota state.
    """

    name: ClassVar[str]

    @abstractmethod
    async def fetch(self) -> ProviderFetch: ...


@runtime_checkable
class SupportsHistory(Protocol):
    """A provider that also exposes endpoints describing past usage.

    Collected on its own slower schedule: these endpoints restate the same
    months of history on every call, so polling them at the quota cadence
    would rewrite an unchanged year every five minutes.
    """

    async def fetch_history(self) -> list[HistoryObservation]: ...
