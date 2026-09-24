from typing import Annotated, Literal

import pytest
import pytest_bazel
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from util.settings_contract import checked_value, cli_args, env_name, settings_file


class _Push(BaseModel):
    private_key_pem: str
    subject: str


class _Server(BaseModel):
    client_id: str


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTRACT_TEST_", env_nested_delimiter="__", cli_kebab_case=True)

    listen_port: int = 8080
    allowed_namespaces: frozenset[str] = frozenset()
    push: _Push | None = None
    servers: dict[str, _Server] = {}


class _Bearer(BaseModel):
    kind: Literal["bearer"] = "bearer"
    token: SecretStr


class _Anonymous(BaseModel):
    kind: Literal["anonymous"] = "anonymous"


type _Auth = Annotated[_Bearer | _Anonymous, Field(discriminator="kind")]


class _Backend(BaseModel):
    url: str
    auth: _Auth


class _Catalog(BaseModel):
    backends: dict[str, _Backend] = {}
    default: str

    @model_validator(mode="after")
    def _default_is_a_backend(self) -> _Catalog:
        if self.default not in self.backends:
            raise ValueError(f"default {self.default!r} is not a backend")
        return self


class _CatalogSettings(_Catalog, BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTRACT_TEST_", env_nested_delimiter="__")


class _AliasedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTRACT_TEST_")

    api_key: str | None = Field(default=None, validation_alias="CONTRACT_TEST_APIKEY")


def test_env_names_follow_the_model_through_optional_and_dict_fields() -> None:
    assert env_name(_Settings, "listen_port") == "CONTRACT_TEST_LISTEN_PORT"
    assert env_name(_Settings, "push", "private_key_pem") == "CONTRACT_TEST_PUSH__PRIVATE_KEY_PEM"
    assert env_name(_Settings, "servers", "github", "client_id") == "CONTRACT_TEST_SERVERS__GITHUB__CLIENT_ID"


def test_env_names_follow_a_pep695_discriminated_union_to_the_member_that_has_the_field() -> None:
    assert (
        env_name(_CatalogSettings, "backends", "tana", "auth", "token") == "CONTRACT_TEST_BACKENDS__TANA__AUTH__TOKEN"
    )
    with pytest.raises(KeyError):
        env_name(_CatalogSettings, "backends", "tana", "auth", "tokn")


def test_an_aliased_field_is_read_under_the_name_env_name_gives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_name(_AliasedSettings, "api_key"), "from-env")
    assert _AliasedSettings().api_key == "from-env"


@pytest.mark.parametrize("path", [("listen_port", "nested"), ("no_such_field",), ("push", "no_such_field")])
def test_a_field_the_model_does_not_have_is_refused(path: tuple[str, ...]) -> None:
    with pytest.raises((KeyError, TypeError)):
        env_name(_Settings, *path)


def test_flags_render_kebab_case_in_call_order_after_type_checks() -> None:
    assert cli_args(_Settings, listen_port=9090, allowed_namespaces=["a"]) == [
        "--listen-port=9090",
        "--allowed-namespaces=['a']",
    ]
    with pytest.raises(ValidationError):
        cli_args(_Settings, listen_port="not-a-port")
    with pytest.raises(KeyError):
        cli_args(_Settings, listen_prot=9090)


def test_settings_file_checks_each_key_against_its_field() -> None:
    content = {"allowed_namespaces": ["a", "b"], "servers": {"github": {"client_id": "id"}}}
    assert settings_file(_Settings, content) is content
    with pytest.raises(ValidationError):
        settings_file(_Settings, {"listen_port": "not-a-port"})
    with pytest.raises(KeyError):
        settings_file(_Settings, {"allowed_namespace": ["a"]})
    with pytest.raises(KeyError):
        settings_file(_Settings, {"servers": {"github": {"client": "id"}}})


def test_a_nested_model_another_source_completes_is_checked_only_for_what_is_present() -> None:
    settings_file(_Settings, {"push": {"subject": "mailto:push@contract.test"}})
    with pytest.raises(ValidationError):
        settings_file(_Settings, {"push": {"subject": 1}})
    with pytest.raises(ValidationError, match="private_key_pem"):
        checked_value(_Settings, "push", {"subject": "mailto:push@contract.test"})


def test_a_discriminated_union_member_is_checked_by_its_tag() -> None:
    settings_file(_Catalog, {"backends": {"tana": {"url": "http://tana.test", "auth": {"kind": "bearer"}}}})
    with pytest.raises(KeyError, match="tokn"):
        settings_file(_Catalog, {"backends": {"tana": {"url": "u", "auth": {"kind": "bearer", "tokn": "t"}}}})
    with pytest.raises(KeyError, match="token"):
        settings_file(_Catalog, {"backends": {"tana": {"url": "u", "auth": {"kind": "anonymous", "token": "t"}}}})


def test_supplied_leaves_complete_the_file_so_its_cross_field_rules_run() -> None:
    content = {"default": "tana", "backends": {"tana": {"url": "u", "auth": {"kind": "bearer"}}}}
    supplied = [("backends", "tana", "auth", "token")]
    assert settings_file(_Catalog, content, supplied=supplied) is content
    with pytest.raises(ValidationError, match="not a backend"):
        settings_file(_Catalog, {**content, "default": "grocy"}, supplied=supplied)


if __name__ == "__main__":
    pytest_bazel.main()
