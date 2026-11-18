import pytest
import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from cdk8s_plus_34 import k8s

from cluster.cdk8s.container_resources import container_resources


@pytest.mark.parametrize(("cpu_limit", "limits"), [(None, {"memory": "512Mi"}), ("1", {"cpu": "1", "memory": "512Mi"})])
def test_cpu_limit_is_rendered_only_when_given(
    scratch_chart: Chart, cpu_limit: str | None, limits: dict[str, str]
) -> None:
    resources = container_resources(cpu="50m", memory="128Mi", cpu_limit=cpu_limit, memory_limit="512Mi")
    k8s.KubePod(
        scratch_chart,
        "pod",
        spec=k8s.PodSpec(containers=[k8s.Container(name="app", image="app.example.test/app", resources=resources)]),
    )
    [pod] = Cdk8sTesting.synth(scratch_chart)
    assert pod["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": limits,
    }


if __name__ == "__main__":
    pytest_bazel.main()
