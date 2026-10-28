"""Python authors and the Go loader enforce the same protobuf schema."""

import protovalidate
import pytest
import pytest_bazel

from agentplane.egress.sidecar.configpb import config_pb2
from util.protobuf import message_dict

# Generated message stubs import protobuf runtime types.
# gazelle:include_dep @pypi//protobuf


def test_serialization_preserves_map_keys_and_leaves_defaults_to_the_consumer() -> None:
    config = config_pb2.Config(
        proxy_host="proxy.example.test",
        token_file="/token",
        audience_token_files={"https://example.test/Audience": "/upper", "https://example.test/audience": "/lower"},
        platform_routes={
            "Control.Route": config_pb2.PlatformRoute(listen_address="127.0.0.1:3130", upstream="gateway:80")
        },
    )
    protovalidate.validate(config)
    rendered = message_dict(config)
    assert rendered["proxy_host"] == "proxy.example.test"
    assert rendered["audience_token_files"] == {
        "https://example.test/Audience": "/upper",
        "https://example.test/audience": "/lower",
    }
    assert rendered["platform_routes"]["Control.Route"]["upstream"] == "gateway:80"
    assert "proxy_port" not in rendered
    assert "listen_address" not in rendered


@pytest.mark.parametrize(
    "config",
    [
        config_pb2.Config(proxy_host="proxy.example.test"),
        config_pb2.Config(proxy_host="proxy.example.test", token_file="/token", proxy_port=0),
        config_pb2.Config(
            proxy_host="proxy.example.test",
            token_file="/token",
            platform_routes={"control": config_pb2.PlatformRoute(listen_address="127.0.0.1:3130")},
        ),
    ],
)
def test_schema_rejects_invalid_configuration(config: config_pb2.Config) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(config)


if __name__ == "__main__":
    pytest_bazel.main()
