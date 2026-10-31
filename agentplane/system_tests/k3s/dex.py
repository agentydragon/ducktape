"""Dex, the k3s fixture's OIDC provider: its config, Deployment and Service. The fixture writes
the config Secret itself. Staging federates directly to the shared Authentik.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    PodSecurityContextProps,
    Secret,
    Service,
    Volume,
)
from constructs import Construct
from pydantic import AnyHttpUrl

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from agentplane.app.action_federation_settings import DirectFederationSettings
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from cluster.cdk8s import pod_policy
from cluster.cdk8s.agentplane import actions, app as app_component
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAME = "dex"
# The Dex Pods' `app.kubernetes.io/name` label value, not an object name.
_POD_LABEL = "systest-dex"
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=5556),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", _POD_LABEL),)),
)
# The client Secrets this Dex reads, which the app and the Action Service read too.
_OIDC = app_component.oidc_secret(NAMESPACE)
_MCP_OAUTH = actions.mcp_oauth_secret(NAMESPACE)
# The environment variables the static clients' `secretEnv` name, set from those Secrets.
_APP_LOGIN_SECRET_ENV = "DEX_CLIENT_SECRET"
_MCP_LINKAGE_SECRET_ENV = "MCP_CLIENT_SECRET"
_MCP_EDGE_LOGIN_SECRET_ENV = "MCP_EDGE_CLIENT_SECRET"
_CONFIG_DIR = "/etc/dex"
CONFIG_KEY = "config.yaml"
# Where Dex serves TLS, beside its plain-HTTP port.
HTTPS_PORT = Port(name="https", number=5554)
_TLS_DIR = "/etc/dex-tls"
# The connector of Dex's password database; part of every subject it issues.
_PASSWORD_CONNECTOR = "local"


def service() -> ServiceRef:
    """The Dex backend, including its Gateway target port."""
    return _SERVICE


def issuer(origin: str) -> str:
    """The issuer of a Dex served at `origin`, which Dex serves under `/dex`."""
    return f"{origin}/dex"


def operator_oidc(*, issuer: str, app_client_id: str) -> OperatorOidcSettings:
    """The Action Service's view of operators logged in to the app's Dex client `app_client_id`."""
    return OperatorOidcSettings(
        issuer=issuer, audience=app_client_id, jwks_uri=f"{issuer}/keys", token_profile=OperatorTokenProfile.DEX
    )


def action_federation(target: OperatorOidcSettings) -> DirectFederationSettings:
    """The app hands the Action Service the operator's own Dex token, which `target` verifies."""
    return DirectFederationSettings(
        mode="direct",
        service_url=AnyHttpUrl(actions.service(NAMESPACE).url),
        login_jwks_uri=target.jwks_uri,
        login_token_profile=OperatorTokenProfile.DEX,
        target=target,
        scope="openid",
    )


@dataclass(frozen=True)
class StaticClient:
    id: str
    name: str
    redirect_uri: str


@dataclass(frozen=True)
class Operator:
    """Dex's one password-database user: the system tests' operator."""

    email: str
    username: str
    user_id: str

    @property
    def subject(self) -> str:
        """The `sub` Dex v2.45 issues this user: `IDTokenSubject{user_id, conn_id}` as unpadded
        base64url protobuf, not the bare user ID."""
        encoded = _length_delimited(1, self.user_id) + _length_delimited(2, _PASSWORD_CONNECTOR)
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


def _length_delimited(number: int, value: str) -> bytes:
    data = value.encode()
    length = len(data)
    varint = bytearray()
    while length > 0x7F:
        varint.append(length & 0x7F | 0x80)
        length >>= 7
    varint.append(length)
    return bytes([number << 3 | 2]) + bytes(varint) + data


def config_yaml(
    *,
    issuer: str,
    app_login: StaticClient,
    mcp_linkage: StaticClient,
    mcp_edge_login: StaticClient,
    operator: Operator,
    password_hash: str,
) -> str:
    """Dex's config: an in-memory store; the app's login client, whose secret comes from
    `app.oidc_secret`; the Action Service's MCP linkage client, whose secret comes from
    `actions.mcp_oauth_secret`; the MCP edge's login client, whose secret comes from the `Dex`'s
    `mcp_edge_login_secret_name`; and `operator`, whose password has the bcrypt `password_hash`.
    Dex serves plain HTTP and `HTTPS_PORT`, whose certificate comes from the `Dex`'s `tls_secret`."""
    clients = [
        (app_login, _APP_LOGIN_SECRET_ENV),
        (mcp_linkage, _MCP_LINKAGE_SECRET_ENV),
        (mcp_edge_login, _MCP_EDGE_LOGIN_SECRET_ENV),
    ]
    return yaml_config(
        {
            "issuer": issuer,
            "storage": {"type": "memory"},
            "web": {
                "http": f"0.0.0.0:{_SERVICE.pod_port}",
                "https": f"0.0.0.0:{HTTPS_PORT.number}",
                "tlsCert": f"{_TLS_DIR}/tls.crt",
                "tlsKey": f"{_TLS_DIR}/tls.key",
            },
            "oauth2": {"skipApprovalScreen": True},
            "enablePasswordDB": True,
            "staticClients": [
                {"id": client.id, "name": client.name, "secretEnv": secret_env, "redirectURIs": [client.redirect_uri]}
                for client, secret_env in clients
            ],
            "staticPasswords": [
                {
                    "email": operator.email,
                    "hash": password_hash,
                    "username": operator.username,
                    "preferredUsername": operator.username,
                    "userID": operator.user_id,
                }
            ],
        }
    )


def operator_login_data(operator: Operator, *, password: str, issuer: str) -> dict[str, str]:
    """The operator's login as `agentplane/system_tests/operator_login.py` reads it from its Secret."""
    return {
        "login": operator.email,
        "username": operator.username,
        "password": password,
        "issuer": issuer,
        "subject": operator.subject,
    }


class Dex(Construct):
    """Dex's Deployment and Service, serving `config_yaml`'s config from the Secret `config_secret`,
    the client secrets of `app.oidc_secret`, `actions.mcp_oauth_secret` and the Secret
    `mcp_edge_login_secret_name`, and the certificate of the `kubernetes.io/tls` Secret `tls_secret`
    on `HTTPS_PORT`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        image: str,
        config_secret: str,
        tls_secret: str,
        mcp_edge_login_secret_name: str,
    ) -> None:
        super().__init__(scope, id)
        env_variables = {
            "DEX_CLIENT_ID": _OIDC.key("client-id").env_value(self, "oidc-client-id-ref"),
            _APP_LOGIN_SECRET_ENV: _OIDC.key("client-secret").env_value(self, "oidc-client-secret-ref"),
            _MCP_LINKAGE_SECRET_ENV: _MCP_OAUTH.key("client-secret").env_value(self, "mcp-oauth-client-secret-ref"),
            _MCP_EDGE_LOGIN_SECRET_ENV: SecretRef(namespace=NAMESPACE, name=mcp_edge_login_secret_name)
            .key("client-secret")
            .env_value(self, "mcp-edge-oauth-client-secret-ref"),
        }
        client_secrets = [_OIDC.name, _MCP_OAUTH.name, mcp_edge_login_secret_name]
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=NAMESPACE,
                labels=_SERVICE.pods.selector,
                annotations={
                    # Dex reads its generated config and client secret only at startup.
                    "secret.reloader.stakater.com/reload": ",".join((config_secret, *client_secrets))
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_SERVICE.pods.selector),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1001, group=1001),
        )
        deployment.add_container(
            name="dex",
            image=image,
            # A specific pinned release, not a floating tag -- avoid re-pulling on every restart.
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["dex", "serve", f"{_CONFIG_DIR}/{CONFIG_KEY}"],
            env_variables=env_variables,
            ports=[_SERVICE.port.container_port(), HTTPS_PORT.container_port()],
            readiness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=0, period_seconds=5),
            liveness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)), memory=MemoryResources(request=Size.mebibytes(64))
            ),
            security_context=ContainerSecurityContextProps(
                read_only_root_filesystem=True, capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL])
            ),
        )
        config_volume = Volume.from_secret(
            self,
            "config-volume",
            Secret.from_secret_name(self, "config-secret", config_secret),
            items={CONFIG_KEY: PathMapping(path=CONFIG_KEY)},
        )
        tmp_volume = Volume.from_empty_dir(self, "tmp-volume", "tmp")
        deployment.containers[0].mount(_CONFIG_DIR, config_volume, read_only=True)
        deployment.containers[0].mount("/tmp", tmp_volume)
        tls_volume = Volume.from_secret(self, "tls-volume", Secret.from_secret_name(self, "tls-secret", tls_secret))
        deployment.containers[0].mount(_TLS_DIR, tls_volume, read_only=True)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=_SERVICE.name, namespace=NAMESPACE),
            selector=deployment,
            ports=[_SERVICE.port.service_port(), HTTPS_PORT.service_port()],
        )
