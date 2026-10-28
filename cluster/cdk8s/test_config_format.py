"""Config text preserves source presence while serializing nested typed values."""

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_serializer

from cluster.cdk8s.config_format import json5_config, yaml_config
from util.settings_contract import validate_settings_fragment


class _Peer(BaseModel):
    url: AnyHttpUrl
    token_file: Path
    retries: int = 3
    optional: str | None = None
    account: str = Field(default="app", serialization_alias="accountName")

    @field_serializer("token_file")
    def serialize_token_file(self, value: Path) -> str:
        return f"file:{value.as_posix()}"


class _Settings(BaseModel):
    peer: _Peer
    peers: list[_Peer]
    named_peers: dict[str, _Peer]
    database_url: str


class _Aliased(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    account: str = Field(serialization_alias="accountName")


@pytest.mark.parametrize("format", ["yaml", "json5"])
def test_nested_models_remain_typed_until_rendering(format: str) -> None:
    peer = _Peer(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None, account="app")
    content: dict[str, Any] = {"peer": peer, "peers": [peer], "named_peers": {"worker": peer}}
    fragment = validate_settings_fragment(_Settings, content)
    rendered = yaml.safe_load(yaml_config(fragment)) if format == "yaml" else json.loads(json5_config(fragment))
    expected: dict[str, Any] = {"url": "http://peer.invalid/", "token_file": "file:/tokens/peer", "account": "app"}
    if format == "json5":
        expected["optional"] = None
    assert rendered == {"peer": expected, "peers": [expected], "named_peers": {"worker": expected}}
    assert fragment is content
    assert content["peer"] is peer
    assert content["peers"][0] is peer
    assert peer.model_fields_set == {"url", "token_file", "optional", "account"}


@pytest.mark.parametrize("format", ["yaml", "json5"])
def test_complete_model_uses_the_same_serialization_boundary(format: str) -> None:
    peer = _Peer(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), retries=3)
    rendered = yaml.safe_load(yaml_config(peer)) if format == "yaml" else json.loads(json5_config(peer))
    assert rendered == {"url": "http://peer.invalid/", "token_file": "file:/tokens/peer", "retries": 3}


@pytest.mark.parametrize("format", ["yaml", "json5"])
def test_models_control_their_serialization_aliases(format: str) -> None:
    content = {"peer": _Aliased(account="app")}
    rendered = yaml.safe_load(yaml_config(content)) if format == "yaml" else json.loads(json5_config(content))
    assert rendered == {"peer": {"accountName": "app"}}


def test_yaml_keeps_cdk8s_null_omission() -> None:
    peer = _Peer(url=AnyHttpUrl("http://peer.invalid"), token_file=Path("/tokens/peer"), optional=None)
    assert yaml.safe_load(yaml_config({"peer": peer, "optional": None}, exclude_none=True)) == {
        "peer": {"url": "http://peer.invalid/", "token_file": "file:/tokens/peer"}
    }


if __name__ == "__main__":
    pytest_bazel.main()
