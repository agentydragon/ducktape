import litellm
import pytest


@pytest.fixture
def isolated_litellm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot litellm's global provider registries for one test.

    Tests that register the Tana custom provider reassign or extend module-level
    litellm state; copy it here so monkeypatch restores the originals on teardown.
    """
    monkeypatch.setattr(litellm, "custom_provider_map", list(litellm.custom_provider_map))
    monkeypatch.setattr(litellm, "provider_list", list(litellm.provider_list))
    monkeypatch.setattr(litellm, "_custom_providers", list(litellm._custom_providers))
    monkeypatch.setattr(litellm, "model_list_set", set(litellm.model_list_set))


@pytest.fixture
def unregistered_tana_provider(isolated_litellm_provider: None) -> None:
    """Start registration tests before LiteLLM knows about Tana."""
    litellm.custom_provider_map = []
    litellm.provider_list = [provider for provider in litellm.provider_list if provider != "tana"]
    litellm._custom_providers = [provider for provider in litellm._custom_providers if provider != "tana"]
    litellm.model_list_set.discard("tana")
