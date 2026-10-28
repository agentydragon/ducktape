"""The same annotation rules must execute in the Python synthesis validator and Go loader."""

import protovalidate
import pytest
import pytest_bazel

from agentplane.llm_ingress.configpb import config_pb2
from util.protobuf import message_dict

# gazelle:include_dep @pypi//protobuf


def test_source_fragment_serializes_without_claiming_complete_validation() -> None:
    content = config_pb2.Config(
        allowed_service_account_namespaces=["config-test"],
        models=[config_pb2.ModelConfig(model="test/model", total_context_budget_tokens=262144)],
        attribution=config_pb2.AttributionConfig(
            json_headers=[
                config_pb2.AttributionJsonHeader(
                    name="X-Test-Verified",
                    fields=[config_pb2.AttributionField(key="Pod.Name", source=config_pb2.pod_name)],
                )
            ]
        ),
    )
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(content)
    rendered = message_dict(content)
    assert "litellm_key" not in rendered
    assert "litellm_url" not in rendered
    assert "token_audience" not in rendered
    assert rendered["attribution"]["json_headers"][0]["fields"][0] == {"key": "Pod.Name", "source": "pod_name"}
    assert content.litellm_key == ""


@pytest.mark.parametrize(
    "attribution",
    [
        config_pb2.AttributionConfig(),
        config_pb2.AttributionConfig(
            baggage=[config_pb2.AttributionField(key="pod", source=config_pb2.VERIFIED_CLAIM_UNSPECIFIED)]
        ),
        config_pb2.AttributionConfig(
            baggage=[config_pb2.AttributionField(key="invalid key", source=config_pb2.pod_name)]
        ),
        config_pb2.AttributionConfig(
            baggage=[config_pb2.AttributionField(key="same", source=config_pb2.namespace)] * 2
        ),
        *[
            config_pb2.AttributionConfig(
                json_headers=[
                    config_pb2.AttributionJsonHeader(
                        name=name, fields=[config_pb2.AttributionField(key="pod", source=config_pb2.pod_name)]
                    )
                ]
            )
            for name in ["X-Api-Key", "X-Forwarded-For", "X-Original-Url", "Authorization", "X-Invalid Header"]
        ],
        config_pb2.AttributionConfig(
            json_headers=[
                config_pb2.AttributionJsonHeader(
                    name=name, fields=[config_pb2.AttributionField(key="pod", source=config_pb2.pod_name)]
                )
                for name in ["X-Test-Verified", "x-test-verified"]
            ]
        ),
    ],
)
def test_synthesis_refuses_unsafe_attribution(attribution: config_pb2.AttributionConfig) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(attribution)


if __name__ == "__main__":
    pytest_bazel.main()
