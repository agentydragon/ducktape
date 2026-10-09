import pytest
import pytest_bazel

from devinfra.ci.skills.ci_latency.scripts import attribution


def test_shared_setup_and_overlapping_triggers():
    result = attribution.calculate(
        {
            "resource": "runner-seconds",
            "runs": [
                {
                    "id": "run-1",
                    "source_commit": "sha",
                    "units": [
                        {"name": "provision", "kind": "provision", "seconds": 90, "triggers": ["a", "b"]},
                        {"name": "test", "kind": "test", "seconds": 30, "triggers": ["b", "c"]},
                    ],
                }
            ],
        }
    )
    assert result["measured_seconds"] == 120
    assert {x["trigger"]: x["seconds"] for x in result["attribution"]} == {"a": 45, "b": 60, "c": 15}


@pytest.mark.parametrize(("triggers", "seconds"), [([], 3), (["a", "a"], 3), (["a"], -1), (["a"], float("nan"))])
def test_bad_values(triggers, seconds):
    with pytest.raises(ValueError, match=r"unit seconds|unit needs|duplicate trigger"):
        attribution.calculate(
            {
                "resource": "worker-seconds",
                "runs": [
                    {
                        "id": "x",
                        "source_commit": "sha",
                        "units": [{"name": "u", "kind": "test", "seconds": seconds, "triggers": triggers}],
                    }
                ],
            }
        )


if __name__ == "__main__":
    pytest_bazel.main()
