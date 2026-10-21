"""Dex, the OIDC provider of agentplane-testing and of the k3s fixture: its config, Deployment and
Service. Testing adds the ESO-generated credentials (3 Password generators + 2 ExternalSecrets, one
carrying the inline Dex config.yaml), HTTPRoute and NetworkPolicy with `add_platform_resources`; the
k3s fixture writes the config Secret itself. Staging federates directly to the shared Authentik.
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
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromRewrite,
    ExternalSecretSpecDataFromRewriteRegexp,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)

from cluster.cdk8s import cilium, pod_policy
from cluster.cdk8s.agentplane import actions, app as app_component
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.minted_secret import password_generator
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy, deny_all_egress
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAMESPACE = "agentplane-testing"
_NAME = "dex"
# The Dex Pods' `app.kubernetes.io/name` label value, not an object name.
_POD_LABEL = "agentplane-testing-dex"
# Keep in step with MODULE.bazel's `dex` pull, the build the k3s fixture runs.
_IMAGE = "ghcr.io/dexidp/dex:v2.45.1"
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=5556),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _POD_LABEL),)),
)
# The client Secrets this Dex reads, which the app and (for the k3s fixture's MCP linkage) the Action
# Service read too.
_OIDC = app_component.oidc_secret(_NAMESPACE)
_MCP_OAUTH = actions.mcp_oauth_secret(_NAMESPACE)
# The environment variables the static clients' `secretEnv` name, set from those Secrets.
_APP_LOGIN_SECRET_ENV = "DEX_CLIENT_SECRET"
_MCP_LINKAGE_SECRET_ENV = "MCP_CLIENT_SECRET"
_ACCEPTANCE_OPERATOR = SecretRef(namespace=_NAMESPACE, name="agentplane-testing-acceptance-operator")
_ISSUER = "https://agentplane-dex-testing.allegedly.works/dex"
_APP_ORIGIN = "https://agentplane-testing.allegedly.works"
_CONFIG_DIR = "/etc/dex"
CONFIG_KEY = "config.yaml"
# Where Dex serves TLS, beside its plain-HTTP port, given a certificate (`Dex(tls_secret=...)`).
HTTPS_PORT = Port(name="https", number=5554)
_TLS_DIR = "/etc/dex-tls"
# The connector of Dex's password database; part of every subject it issues.
_PASSWORD_CONNECTOR = "local"
_CPU_LIMIT = Cpu.millis(100)
_MEMORY_LIMIT = Size.mebibytes(128)


def service() -> ServiceRef:
    """The Dex backend, including its Gateway target port."""
    return _SERVICE


def issuer(origin: str) -> str:
    """The issuer of a Dex served at `origin`, which Dex serves under `/dex`."""
    return f"{origin}/dex"


@dataclass(frozen=True)
class StaticClient:
    id: str
    name: str
    redirect_uri: str


@dataclass(frozen=True)
class Operator:
    """Dex's one password-database user: the acceptance suite's operator."""

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


# Shared between Dex's own staticPasswords entry and the ExternalSecret template that records
# its login, so the two can't name different identities.
_TESTING_OPERATOR = Operator(email="test-user@agentplane-testing.invalid", username="test-user", user_id="test-subject")


def config_yaml(
    *,
    issuer: str,
    app_login: StaticClient,
    operator: Operator,
    password_hash: str,
    mcp_linkage: StaticClient | None = None,
    serve_tls: bool = False,
) -> str:
    """Dex's config: an in-memory store, the app's login client, whose secret comes from
    `app.oidc_secret`, and `operator`, whose password has the bcrypt `password_hash`. `mcp_linkage`
    adds the Action Service's MCP linkage client, whose secret comes from `actions.mcp_oauth_secret`,
    for a Dex given `mcp_linkage`; `serve_tls` adds `HTTPS_PORT`, for a Dex given `tls_secret`."""
    web = {"http": f"0.0.0.0:{_SERVICE.pod_port}"}
    if serve_tls:
        web |= {
            "https": f"0.0.0.0:{HTTPS_PORT.number}",
            "tlsCert": f"{_TLS_DIR}/tls.crt",
            "tlsKey": f"{_TLS_DIR}/tls.key",
        }
    clients = [(app_login, _APP_LOGIN_SECRET_ENV)]
    if mcp_linkage is not None:
        clients.append((mcp_linkage, _MCP_LINKAGE_SECRET_ENV))
    return yaml_config(
        {
            "issuer": issuer,
            "storage": {"type": "memory"},
            "web": web,
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


def _testing_config_yaml() -> str:
    return config_yaml(
        issuer=_ISSUER,
        app_login=StaticClient(
            id="agentplane-testing", name="Agentplane testing", redirect_uri=f"{_APP_ORIGIN}/auth/callback"
        ),
        operator=_TESTING_OPERATOR,
        # The ExternalSecret template hashes the generated password.
        password_hash=(
            f'{{{{ regexReplaceAll "^{_TESTING_OPERATOR.username}:" '
            f'(htpasswd "{_TESTING_OPERATOR.username}" .password "bcrypt") "" }}}}'
        ),
    )


def _add_credentials(scope: Construct) -> None:
    operator_password = password_generator(
        scope,
        "dex-operator-password-generator",
        name="agentplane-testing-dex-operator-password",
        namespace=_NAMESPACE,
        length=48,
        digits=12,
    )
    client_secret = password_generator(
        scope,
        "dex-client-secret-generator",
        name="agentplane-testing-dex-client-secret",
        namespace=_NAMESPACE,
        length=48,
        digits=12,
    )
    session_secret = password_generator(
        scope,
        "session-secret-generator",
        name="agentplane-testing-agentplane-session-secret",
        namespace=_NAMESPACE,
        length=64,
        digits=16,
    )

    def rewrite(source: str, target: str) -> ExternalSecretSpecDataFrom:
        return DataFrom.from_password_generator(
            source,
            rewrite=[
                ExternalSecretSpecDataFromRewrite(
                    regexp=ExternalSecretSpecDataFromRewriteRegexp(source="password", target=target)
                )
            ],
        )

    ExternalSecret(
        scope,
        "oidc-credentials",
        metadata=ApiObjectMetadata(
            name=_OIDC.name,
            namespace=_OIDC.namespace,
            annotations={"description": "ESO-generated Dex client credentials and Agentplane session signing key."},
        ),
        refresh_interval="8760h",
        data_from=[rewrite(client_secret, "client-secret"), rewrite(session_secret, "session-secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            data={
                "client-id": "agentplane-testing",
                "client-secret": '{{ index . "client-secret" }}',
                "session-secret": '{{ index . "session-secret" }}',
            },
        ),
    )

    ExternalSecret(
        scope,
        "acceptance-operator-credentials",
        metadata=ApiObjectMetadata(
            name=_ACCEPTANCE_OPERATOR.name,
            namespace=_ACCEPTANCE_OPERATOR.namespace,
            annotations={
                "description": "Generates the acceptance password and Dex config together from one password value."
            },
        ),
        refresh_interval="8760h",
        # Dex's config and the acceptance client's password both come from this one
        # dataFrom entry: two ExternalSecrets naming the same Password generator get two
        # independent values (#7042).
        data_from=[DataFrom.from_password_generator(operator_password)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            type="Opaque",
            data={
                **operator_login_data(_TESTING_OPERATOR, password="{{ .password }}", issuer=_ISSUER),
                CONFIG_KEY: _testing_config_yaml(),
            },
        ),
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
    """Dex's Deployment and Service, serving the `config.yaml` of the Secret `config_secret` with the
    client secret of `app.oidc_secret`; given `mcp_linkage`, that of `actions.mcp_oauth_secret` too,
    for a config with an `mcp_linkage` client; given the `kubernetes.io/tls` Secret `tls_secret`, its
    certificate on `HTTPS_PORT` too, for a config that `serve_tls`. Testing's credentials, route and
    network policy are added by `add_platform_resources`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        image: str = _IMAGE,
        config_secret: str = _ACCEPTANCE_OPERATOR.name,
        cpu_limit: Cpu | None = _CPU_LIMIT,
        memory_limit: Size | None = _MEMORY_LIMIT,
        tls_secret: str | None = None,
        mcp_linkage: bool = False,
    ) -> None:
        super().__init__(scope, id)
        env_variables = {
            "DEX_CLIENT_ID": _OIDC.key("client-id").env_value(self, "oidc-client-id-ref"),
            _APP_LOGIN_SECRET_ENV: _OIDC.key("client-secret").env_value(self, "oidc-client-secret-ref"),
        }
        client_secrets = [_OIDC.name]
        if mcp_linkage:
            env_variables[_MCP_LINKAGE_SECRET_ENV] = _MCP_OAUTH.key("client-secret").env_value(
                self, "mcp-oauth-client-secret-ref"
            )
            client_secrets.append(_MCP_OAUTH.name)
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=_NAMESPACE,
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
            ports=[_SERVICE.port.container_port(), *([HTTPS_PORT.container_port()] if tls_secret is not None else [])],
            readiness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=0, period_seconds=5),
            liveness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10), limit=cpu_limit),
                memory=MemoryResources(request=Size.mebibytes(64), limit=memory_limit),
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
        if tls_secret is not None:
            tls_volume = Volume.from_secret(self, "tls-volume", Secret.from_secret_name(self, "tls-secret", tls_secret))
            deployment.containers[0].mount(_TLS_DIR, tls_volume, read_only=True)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=_SERVICE.name, namespace=_NAMESPACE),
            selector=deployment,
            ports=[_SERVICE.port.service_port(), *([HTTPS_PORT.service_port()] if tls_secret is not None else [])],
        )

    def add_platform_resources(self) -> None:
        """Add testing's ESO-generated credentials and config, public route and Cilium policy."""
        _add_credentials(self)
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
            hostnames=["agentplane-dex-testing.allegedly.works"],
            backend=_SERVICE,
        )
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
            endpoint_selector=_SERVICE.pods.selector,
            ingress=[
                IngressRule.from_gateway(_SERVICE.pod_port),
                app_component.service(_NAMESPACE).pods.admit(_SERVICE.pod_port),
            ],
            egress=[cilium.dns_egress()],
            egress_deny=deny_all_egress(),
        )
