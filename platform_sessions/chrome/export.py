"""Common interface for provider-specific Chrome session extraction."""

from pathlib import Path
from typing import Protocol, TypeVar

from platform_sessions.chrome.profile import ChromeProfile

Candidate_co = TypeVar("Candidate_co", covariant=True)


class ChromeExtractor(Protocol[Candidate_co]):
    def extract(self, profile: ChromeProfile) -> Candidate_co:
        """Read local browser state and return provider-specific material."""


def extract_chrome[Candidate](profile: Path, extractor: ChromeExtractor[Candidate]) -> Candidate:
    """Use the selected profile with a provider's extractor."""
    return extractor.extract(ChromeProfile(profile))
