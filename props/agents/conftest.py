"""Container-backed fixtures for tests of the agent implementations."""

from props.testing.fixtures.e2e_container import e2e_stack
from props.testing.fixtures.e2e_infra import (
    _e2e_registry_container,
    critic_dev_improve_image,
    critic_dev_optimize_image,
    critic_image,
    e2e_registry,
    e2e_registry_url,
    grader_image,
)
