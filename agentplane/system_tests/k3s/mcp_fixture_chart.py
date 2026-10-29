"""Add the MCP acceptance fixture to the shared k3s chart."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import bcrypt
from cdk8s import ApiObjectMetadata, Chart, Testing
from cdk8s_plus_34 import ConfigMap, Deployment, EnvValue, ServiceAccount, Volume
from constructs import Construct
from more_itertools import one

from agentplane.action_service.policies.resources import CALLER_LABEL
from agentplane.mcp_edge.oauth_settings import OAuthSettings, OperatorPrincipal
from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests import mcp_servers
from agentplane.system_tests.k3s import dex, fixture_chart, mcp_fixture_servers
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from agentplane.system_tests.k3s.rendered_fixture import McpInputs, Models
from cluster.cdk8s.agentplane import actions

DEX_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-dex:unset"
MCP_EVERYTHING_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-mcp-everything:unset"
OPERATOR_SECRET = "test-operator-login"
_DEX_TLS_SECRET = "test-dex-tls"
_MCP_EDGE_OAUTH_SECRET = "agentplane-mcp-edge-oauth"
_TRUST_BUNDLE = "test-fixture-ca"
_TRUST_BUNDLE_KEY = "ca-certificates.crt"
_TRUST_BUNDLE_DIR = "/etc/test-fixture-ca"
_MCP_EDGE_TEST_CALLER = "mcp-edge-test-caller"
_OPERATOR = dex.Operator(email="test-operator@k3s-fixture.invalid", username="test-operator", user_id="test-operator")
_OPERATOR_PASSWORD = "test-k3s-operator-password"
_MCP_EDGE_LOGIN = dex.StaticClient(
    id="test-mcp-edge-oauth",
    name="Test MCP edge OAuth",
    redirect_uri=f"{fixture_chart.MCP_EDGE_SERVICE.url}/auth/callback",
)


def objects(
    *, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str, models: Models, mcp: McpInputs
) -> list[dict[str, Any]]:
    env = fixture_chart.environment(vm_sandboxes=False, models=models, app_origin=mcp.app_origin)
    mcp_linkage = fixture_chart.linkage_client(mcp.app_origin)
    actions_config = replace(
        env.actions,
        mcp_servers={
            mcp_servers.OAUTH: mcp_fixture_servers.oauth_fixture_server(
                mcp_servers.OAUTH, client_id=mcp_linkage.id, redirect_uri=mcp_linkage.redirect_uri
            )
        },
        action_groups={
            mcp_servers.EVERYTHING_GROUP: mcp_fixture_servers.everything_group(),
            mcp_servers.OAUTH: mcp_fixture_servers.oauth_fixture_group(mcp_servers.OAUTH),
            "sandbox": env.actions.action_groups["sandbox"],
        },
        oauth_secret_items=("client-secret",),
    )
    mcp_edge_config = replace(
        env.mcp_edge,
        mintable_accounts=frozenset({ServiceAccountRef(namespace=NAMESPACE, name=_MCP_EDGE_TEST_CALLER)}),
        oauth_secret_name=_MCP_EDGE_OAUTH_SECRET,
    )
    env = replace(env, actions=actions_config, mcp_edge=mcp_edge_config)
    fixture = fixture_chart.build_chart(
        ca_cert=ca_cert, ca_key=ca_key, system_roots=system_roots, cluster_ca=cluster_ca, models=models, env=env
    )
    mcp_edge_login = replace(_MCP_EDGE_LOGIN, redirect_uri=f"{mcp.mcp_edge_origin}/auth/callback")
    edge_oauth = _mcp_edge_oauth(mcp.mcp_edge_origin, mcp.app_origin, mcp_edge_login.id)
    fixture_chart.add_secret(
        fixture.chart,
        _MCP_EDGE_OAUTH_SECRET,
        {"client-secret": "test-k3s-mcp-edge-client-secret", "oauth": edge_oauth.model_dump_json()},
    )
    ServiceAccount(
        fixture.chart,
        "mcp-edge-test-caller",
        metadata=ApiObjectMetadata(name=_MCP_EDGE_TEST_CALLER, namespace=NAMESPACE, labels={CALLER_LABEL: "true"}),
        automount_token=False,
    )
    _add_mcp_fixtures(
        fixture.chart,
        mcp,
        app_login=fixture.app_login,
        mcp_linkage=fixture.mcp_linkage,
        mcp_edge_login=mcp_edge_login,
        system_roots=system_roots,
        dex_clients=[fixture.app, fixture.actions, fixture.mcp_edge],
    )
    return list(Testing.synth(fixture.chart))


def _add_mcp_fixtures(
    chart: Chart,
    mcp: McpInputs,
    *,
    app_login: dex.StaticClient,
    mcp_linkage: dex.StaticClient,
    mcp_edge_login: dex.StaticClient,
    system_roots: str,
    dex_clients: list[Construct],
) -> None:
    """Add Dex, its TLS trust and the MCP servers the Action groups call."""
    password_hash = bcrypt.hashpw(_OPERATOR_PASSWORD.encode(), b"$2a$10$abcdefghijklmnopqrstuu").decode()
    fixture_chart.add_secret(
        chart,
        OPERATOR_SECRET,
        {
            **dex.operator_login_data(_OPERATOR, password=_OPERATOR_PASSWORD, issuer=fixture_chart.DEX_ISSUER),
            dex.CONFIG_KEY: dex.config_yaml(
                issuer=fixture_chart.DEX_ISSUER,
                app_login=app_login,
                mcp_linkage=mcp_linkage,
                mcp_edge_login=mcp_edge_login,
                operator=_OPERATOR,
                password_hash=password_hash,
            ),
        },
    )
    fixture_chart.add_secret(
        chart, _DEX_TLS_SECRET, {"tls.crt": mcp.dex_cert, "tls.key": mcp.dex_key}, kind="kubernetes.io/tls"
    )
    trust_bundle = ConfigMap(
        chart,
        "trust-bundle",
        metadata=ApiObjectMetadata(name=_TRUST_BUNDLE, namespace=NAMESPACE),
        data={_TRUST_BUNDLE_KEY: system_roots + mcp.dex_ca_cert},
    )
    for client in dex_clients:
        _trust(client, trust_bundle)
    dex.Dex(
        chart,
        "dex",
        image=DEX_IMAGE,
        config_secret=OPERATOR_SECRET,
        tls_secret=_DEX_TLS_SECRET,
        mcp_edge_login_secret_name=_MCP_EDGE_OAUTH_SECRET,
    )
    mcp_fixture_servers.add_mcp_everything(chart, image=MCP_EVERYTHING_IMAGE)
    mcp_fixture_servers.add_oauth_fixture(
        chart,
        authorization_server=fixture_chart.DEX_ISSUER,
        # The fixture fetches signing keys over Dex's plain-http port.
        jwks_uri=f"{dex.issuer(dex.service().url)}/keys",
        audience=mcp_linkage.id,
    )


def _mcp_edge_oauth(origin: str, app_origin: str, upstream_client_id: str) -> OAuthSettings:
    """The standalone edge's OAuth server at `origin`, using its own registered client at Dex."""
    files = Path(actions.MCP_OAUTH_DIR)
    return OAuthSettings(
        config_url=f"{fixture_chart.DEX_ISSUER}/.well-known/openid-configuration",
        upstream_client_id=upstream_client_id,
        upstream_client_secret_file=files / "client-secret",
        base_url=origin,
        integration_app_url=app_origin,
        # These legacy keys are still accepted by the shared OAuth settings model; the edge does not read them.
        jwt_signing_key_file=files / "unused-jwt-signing-key",
        encryption_key_file=files / "unused-encryption-key",
        upstream_issuer=fixture_chart.DEX_ISSUER,
        upstream_subject=_OPERATOR.subject,
        approving_operator=OperatorPrincipal(issuer=fixture_chart.DEX_ISSUER, subject=_OPERATOR.subject),
    )


def _trust(workload: Construct, bundle: ConfigMap) -> None:
    """The Python clients in a workload verify Dex with `bundle`."""
    container = one(one(child for child in workload.node.find_all() if isinstance(child, Deployment)).containers)
    container.mount(_TRUST_BUNDLE_DIR, Volume.from_config_map(workload, "trust-bundle", bundle), read_only=True)
    container.env.add_variable("SSL_CERT_FILE", EnvValue.from_value(f"{_TRUST_BUNDLE_DIR}/{_TRUST_BUNDLE_KEY}"))
