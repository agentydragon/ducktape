"""Config text preserves mapping keys and serializes complete nested typed values."""

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
)

from cluster.cdk8s.config_format import json_config, yaml_config
from cluster.rotators.attic_jwt_rotation.config import (
    Config,
    KubernetesDestination,
    KubernetesSecretDestination,
    SopsDestination,
    Token,
    WorkspaceNotification,
)
from util.settings_contract import validate_settings_fragment


class Peer(BaseModel):
    url: AnyHttpUrl
    token_file: Path
    retries: int = 3
    optional: str | None = None
    account: str = Field(default="app", serialization_alias="accountName")

    @field_serializer("token_file")
    def serialize_token_file(self, value: Path) -> str:
        return f"file:{value.as_posix()}"

    @model_serializer(mode="wrap", when_used="json")
    def serialize_entry(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        serialized["serialized"] = True
        return serialized


class Settings(BaseModel):
    peer: Peer
    peers: list[Peer]
    named_peers: dict[str, Peer]
    database_url: str


class AliasedPeer(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    account: str = Field(serialization_alias="accountName")


class ExtendedPeer(Peer):
    additional: str


class FloatSettings(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="strings")
    value: float


def _render(format: str, data: BaseModel | dict[str, Any]) -> dict[str, Any]:
    decoded = yaml.safe_load(yaml_config(data)) if format == "yaml" else json.loads(json_config(data))
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.parametrize("nested", [False, True])
def test_json_preserves_model_number_encoding(nested: bool) -> None:
    settings = FloatSettings(value=float("inf"))
    rendered = json_config({"settings": settings} if nested else settings)
    expected = {"value": "Infinity"}
    assert json.loads(rendered) == ({"settings": expected} if nested else expected)


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_complete_and_nested_models_use_their_serializers(format: str) -> None:
    peer = Peer(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None, account="app")
    content: dict[str, Any] = {"peer": peer, "peers": [peer], "named_peers": {"worker": peer}}
    fragment = validate_settings_fragment(Settings, content)
    expected: dict[str, Any] = {
        "url": "http://peer.invalid/",
        "token_file": "file:/tokens/peer",
        "account": "app",
        "retries": 3,
        "serialized": True,
    }
    if format == "json":
        expected["optional"] = None
    assert _render(format, peer) == expected
    assert _render(format, fragment) == {"peer": expected, "peers": [expected], "named_peers": {"worker": expected}}
    assert fragment is content
    assert content["peer"] is peer
    assert content["peers"][0] is peer
    assert peer.model_fields_set == {"url", "token_file", "optional", "account"}


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_models_control_their_serialization_aliases(format: str) -> None:
    content = {"peer": AliasedPeer(account="app")}
    rendered = _render(format, content)
    assert rendered == {"peer": {"accountName": "app"}}


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_serialization_retains_nested_field_schema(format: str) -> None:
    entry = ExtendedPeer(
        url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), additional="subclass-only"
    )
    settings = Settings(peer=entry, peers=[entry], named_peers={"worker": entry}, database_url="test-db")
    rendered = _render(format, settings)
    # A field typed as Peer serializes only the base schema, including inside a list or map.
    expected = _render(format, entry)
    assert expected["additional"] == "subclass-only"
    del expected["additional"]
    assert rendered == {
        "peer": expected,
        "peers": [expected],
        "named_peers": {"worker": expected},
        "database_url": "test-db",
    }


def test_yaml_keeps_cdk8s_null_omission() -> None:
    peer = Peer(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None)
    result = yaml.safe_load(yaml_config({"peer": peer, "optional": None}, exclude_none=True))
    assert set(result) == {"peer"}
    assert result["peer"] == _render("yaml", peer)
    assert "optional" not in result["peer"]


@pytest.mark.parametrize("format", ["yaml", "json"])
@pytest.mark.parametrize("nested", [False, True])
def test_config_preserves_default_destination_discriminators(format: str, nested: bool) -> None:
    config = Config(
        tokens=[
            Token(
                name="reader",
                sub="reader",
                validity="1 year",
                pull=["main"],
                destination=SopsDestination(sops_file=Path("reader.sops.yaml")),
            ),
            Token(
                name="writer",
                sub="writer",
                validity="1 year",
                pull=["main"],
                destination=KubernetesDestination(
                    secret=KubernetesSecretDestination(namespace="nix-cache", name="attic-token", key="token"),
                    notification=WorkspaceNotification(
                        workspace="github-secrets-sync", annotation_key="test/token-version"
                    ),
                ),
            ),
        ]
    )

    content = {"settings": config} if nested else config
    rendered = _render(format, content)
    settings = rendered["settings"] if nested else rendered

    assert [token["destination"]["storage"] for token in settings["tokens"]] == ["sops", "kubernetes"]
    parsed = Config.model_validate(settings)
    assert isinstance(parsed.tokens[0].destination, SopsDestination)
    assert isinstance(parsed.tokens[1].destination, KubernetesDestination)


if __name__ == "__main__":
    pytest_bazel.main()
