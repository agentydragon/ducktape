import pytest
import pytest_bazel
from pydantic import AnyHttpUrl, ValidationError

from agentplane.action_service.mcp_settings import McpClientMetadataSettings

_URL = "https://actions.example.test/oauth/client-metadata.json"


def test_metadata_url_is_parsed_and_serializes_as_string() -> None:
    settings = McpClientMetadataSettings.model_validate({"url": _URL, "client_name": "Test"})
    assert isinstance(settings.url, AnyHttpUrl)
    assert settings.model_dump(mode="json")["url"] == _URL
    assert McpClientMetadataSettings.model_validate(settings.model_dump()) == settings


@pytest.mark.parametrize(
    "url",
    [
        "http://actions.example.test/oauth/client-metadata.json",
        "https://actions.example.test:8443/oauth/client-metadata.json",
        "https://actions.example.test:443/oauth/client-metadata.json",
        "https://ACTIONS.example.test/oauth/client-metadata.json",
        "https://actions.example.test/wrong-path",
        _URL + "?query=1",
        _URL + "#fragment",
        "https://user:password@actions.example.test/oauth/client-metadata.json",
    ],
)
def test_metadata_url_rejects_noncanonical_or_disallowed_urls(url: str) -> None:
    with pytest.raises(ValidationError, match=r"URL|url"):
        McpClientMetadataSettings.model_validate({"url": url, "client_name": "Test"})


if __name__ == "__main__":
    pytest_bazel.main()
