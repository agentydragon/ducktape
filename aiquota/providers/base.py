from abc import ABC, abstractmethod
from typing import ClassVar

from aiquota.models import ProviderFetch


class Provider(ABC):
    """One AI subscription whose quota status we can poll.

    Subclasses are constructed with their typed settings from `Config` and
    expose a single `fetch()` method returning the latest quota state.
    """

    name: ClassVar[str]

    @abstractmethod
    def fetch(self) -> ProviderFetch: ...
