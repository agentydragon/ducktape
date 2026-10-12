"""Static model values shared by Agentplane app tests."""

from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.runner.harness import Harness

TEST_REASONING_EFFORTS = ("low", "medium", "high")
# One model per harness, for an app whose tests start no thread on a particular model.
TEST_MODELS = ModelCatalog(
    models=[
        ModelOption(
            model="test-claude-model", display_name="Test Claude Model", reasoning_efforts=list(TEST_REASONING_EFFORTS)
        ),
        ModelOption(
            model="test-codex-model", display_name="Test Codex Model", reasoning_efforts=list(TEST_REASONING_EFFORTS)
        ),
    ],
    harnesses={Harness.CLAUDE: ["test-claude-model"], Harness.CODEX: ["test-codex-model"]},
)
