"""Config text preserves source presence while serializing nested typed values."""

import json
from pathlib import Path
from typing import Any, ClassVar

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


class TestSettingsEntry(BaseModel):
    __test__: ClassVar[bool] = False

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


class TestSettings(BaseModel):
    __test__: ClassVar[bool] = False

    peer: TestSettingsEntry
    peers: list[TestSettingsEntry]
    named_peers: dict[str, TestSettingsEntry]
    database_url: str


class TestAliasedSettingsEntry(BaseModel):
    __test__: ClassVar[bool] = False

    model_config = ConfigDict(serialize_by_alias=True)
    account: str = Field(serialization_alias="accountName")


class TestExtendedSettingsEntry(TestSettingsEntry):
    additional: str


class TestFloatSettings(BaseModel):
    __test__: ClassVar[bool] = False

    model_config = ConfigDict(ser_json_inf_nan="strings")
    value: float


@pytest.mark.parametrize("nested", [False, True])
def test_json_preserves_model_number_encoding(nested: bool) -> None:
    settings = TestFloatSettings(value=float("inf"))
    rendered = json_config({"settings": settings} if nested else settings)
    expected = {"value": "Infinity"}
    assert json.loads(rendered) == ({"settings": expected} if nested else expected)


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_nested_models_remain_typed_until_rendering(format: str) -> None:
    peer = TestSettingsEntry(
        url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None, account="app"
    )
    content: dict[str, Any] = {"peer": peer, "peers": [peer], "named_peers": {"worker": peer}}
    fragment = validate_settings_fragment(TestSettings, content)
    rendered = yaml.safe_load(yaml_config(fragment)) if format == "yaml" else json.loads(json_config(fragment))
    expected: dict[str, Any] = {
        "url": "http://peer.invalid/",
        "token_file": "file:/tokens/peer",
        "account": "app",
        "serialized": True,
    }
    if format == "json":
        expected["optional"] = None
    assert rendered == {"peer": expected, "peers": [expected], "named_peers": {"worker": expected}}
    assert fragment is content
    assert content["peer"] is peer
    assert content["peers"][0] is peer
    assert peer.model_fields_set == {"url", "token_file", "optional", "account"}


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_complete_model_uses_the_same_serialization_boundary(format: str) -> None:
    peer = TestSettingsEntry(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), retries=3)
    rendered = yaml.safe_load(yaml_config(peer)) if format == "yaml" else json.loads(json_config(peer))
    assert rendered == {
        "url": "http://peer.invalid/",
        "token_file": "file:/tokens/peer",
        "retries": 3,
        "serialized": True,
    }


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_models_control_their_serialization_aliases(format: str) -> None:
    content = {"peer": TestAliasedSettingsEntry(account="app")}
    rendered = yaml.safe_load(yaml_config(content)) if format == "yaml" else json.loads(json_config(content))
    assert rendered == {"peer": {"accountName": "app"}}


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_serialization_retains_nested_field_schema(format: str) -> None:
    entry = TestExtendedSettingsEntry(
        url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), additional="subclass-only"
    )
    settings = TestSettings(peer=entry, peers=[entry], named_peers={"worker": entry}, database_url="test-db")
    rendered = yaml.safe_load(yaml_config(settings)) if format == "yaml" else json.loads(json_config(settings))
    expected = {"url": "http://peer.invalid/", "token_file": "file:/tokens/peer", "serialized": True}
    assert rendered == {
        "peer": expected,
        "peers": [expected],
        "named_peers": {"worker": expected},
        "database_url": "test-db",
    }
    assert settings.model_dump(mode="json", exclude_unset=True) == rendered


def test_yaml_keeps_cdk8s_null_omission() -> None:
    peer = TestSettingsEntry(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None)
    assert yaml.safe_load(yaml_config({"peer": peer, "optional": None}, exclude_none=True)) == {
        "peer": {"url": "http://peer.invalid/", "token_file": "file:/tokens/peer", "serialized": True}
    }


def test_yaml_config_preserves_default_destination_discriminators() -> None:
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

    rendered = yaml.safe_load(yaml_config(config.model_dump(mode="json")))

    assert [token["destination"]["storage"] for token in rendered["tokens"]] == ["sops", "kubernetes"]
    parsed = Config.model_validate(rendered)
    assert isinstance(parsed.tokens[0].destination, SopsDestination)
    assert isinstance(parsed.tokens[1].destination, KubernetesDestination)


if __name__ == "__main__":
    pytest_bazel.main()
