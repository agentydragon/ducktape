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
