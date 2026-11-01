import pytest_bazel

from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.env.runtime.harnesshost.harness import Harness


def test_central_sampler_offerings_have_no_service_selection() -> None:
    models = [ModelOption(model="central", display_name="Central", reasoning_efforts=["high"])]
    harnesses = {Harness.CLAUDE: [], Harness.CODEX: [], Harness.CENTRAL_SAMPLER: ["central"]}
    catalog = ModelCatalog(models=models, harnesses=harnesses)
    assert "central_sampler" not in catalog.model_dump()
    assert catalog.harnesses[Harness.CENTRAL_SAMPLER] == ["central"]


def test_native_catalog_does_not_offer_central_sampler_by_default() -> None:
    catalog = ModelCatalog(
        models=[ModelOption(model="native", display_name="Native", reasoning_efforts=[])],
        harnesses={Harness.CLAUDE: ["native"], Harness.CODEX: ["native"]},
    )
    assert Harness.CENTRAL_SAMPLER not in catalog.harnesses


if __name__ == "__main__":
    pytest_bazel.main()
