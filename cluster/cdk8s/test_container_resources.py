import pytest
import pytest_bazel

from cluster.cdk8s.container_resources import container_resources


@pytest.mark.parametrize(("cpu_limit", "limits"), [(None, {"memory": "512Mi"}), ("1", {"cpu": "1", "memory": "512Mi"})])
def test_cpu_limit_is_set_only_when_given(cpu_limit: str | None, limits: dict[str, str]) -> None:
    resources = container_resources(cpu="50m", memory="128Mi", cpu_limit=cpu_limit, memory_limit="512Mi")
    assert resources.requests is not None
    assert {key: amount.value for key, amount in resources.requests.items()} == {"cpu": "50m", "memory": "128Mi"}
    assert resources.limits is not None
    assert {key: amount.value for key, amount in resources.limits.items()} == limits


if __name__ == "__main__":
    pytest_bazel.main()
