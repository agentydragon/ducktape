import json
from typing import Any

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path


def upsert(object_type: str, match_on: list[str], **values: Any) -> dict[str, Any]:
    return {"@type": "upsert", "object": object_type, "matchOn": match_on, "value": values}


def update(object_type: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"@type": "update", "object": object_type, "value": value}


def destroy(object_type: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    operation: dict[str, Any] = {"@type": "destroy", "object": object_type}
    if value is not None:
        operation["value"] = value
    return operation


WHITELIST_SCRIPT = """\
require ["variables", "reject", "envelope"];

if not string :is "${env.spf.result}" "pass" {
    reject "550 5.7.1 sender not SPF-verified (spf=${env.spf.result})";
    stop;
}

if not envelope :comparator "i;ascii-casemap" :is "from" "agentydragon@gmail.com" {
    reject "550 5.7.1 sender not authorized for this mailbox (from=${envelope.from})";
}
"""


EXPECTED_PLAN = [
    upsert(
        "Certificate",
        ["certificate"],
        **{
            "cert-mx": {
                "certificate": {"@type": "File", "filePath": "/tls/tls.crt"},
                "privateKey": {"@type": "File", "filePath": "/tls/tls.key"},
            }
        },
    ),
    upsert("Domain", ["name"], dom={"name": "allegedly.works", "description": "Haku mailbox domain"}),
    destroy("Account", {"name": "stalwart-reconciler"}),
    upsert(
        "Account",
        ["name"],
        **{
            "acct-haku": {
                "@type": "User",
                "name": "haku",
                "domainId": "#dom",
                "description": "Haku background agent (auth: Authentik OIDC bearer only)",
            }
        },
    ),
    upsert(
        "Directory",
        ["description"],
        **{
            "dir-authentik": {
                "@type": "Oidc",
                "description": "Authentik (stalwart-haku provider)",
                "issuerUrl": "https://auth.allegedly.works/application/o/stalwart-haku/",
                "requireAudience": "stalwart-haku",
                "requireScopes": {"openid": True, "email": True},
                "claimUsername": "preferred_username",
                "usernameDomain": "allegedly.works",
                "claimName": "name",
            }
        },
    ),
    upsert(
        "SieveSystemScript",
        ["name"],
        **{
            "script-wl": {
                "name": "operator-whitelist",
                "description": (
                    "Reject delivery unless the SPF-verified envelope sender is on the operator "
                    "whitelist. Whitelist check uses the RFC 5228 envelope test: ${env.from} is "
                    "EMPTY at the DATA-stage hook (verified 0.16.11 — same class as "
                    'env.dmarc.result), while envelope :is "from" evaluates correctly.'
                ),
                "isActive": True,
                "contents": WHITELIST_SCRIPT,
            }
        },
    ),
    destroy("NetworkListener"),
    upsert(
        "NetworkListener",
        ["name"],
        **{
            "lst-smtp": {
                "name": "smtp",
                "protocol": "smtp",
                "bind": {"0.0.0.0:2525": True},
                "overrideProxyTrustedNetworks": {"10.244.0.0/16": True},
                "useTls": True,
                "tlsImplicit": False,
            },
            "lst-http": {"name": "http", "protocol": "http", "bind": {"0.0.0.0:8080": True}, "useTls": False},
            "lst-imap": {"name": "imap", "protocol": "imap", "bind": {"0.0.0.0:1143": True}, "useTls": False},
        },
    ),
    update("Authentication", {"directoryId": "#dir-authentik"}),
    update(
        "MtaStageAuth",
        {"require": {"match": {"0": {"if": "local_port == 2525", "then": "false"}}, "else": "local_port != 25"}},
    ),
    update(
        "SenderAuth",
        {
            "spfFromVerify": {"else": "'relaxed'"},
            "spfEhloVerify": {"else": "'relaxed'"},
            "dmarcVerify": {"else": "'relaxed'"},
        },
    ),
    update("MtaStageData", {"script": {"else": "'operator-whitelist'"}}),
    update("SystemSettings", {"defaultHostname": "mx.allegedly.works", "defaultDomainId": "#dom"}),
]


def test_mailbox_plan_matches_readable_python_specification() -> None:
    plan_path = get_required_path("_main/cluster/k8s/haku/mailbox/app/mailbox-plan.ndjson")
    actual = [json.loads(line) for line in plan_path.read_text().splitlines() if line.strip()]
    assert actual == EXPECTED_PLAN


def test_mailbox_initialization_is_serialized_and_init_only() -> None:
    deployment_path = get_required_path("_main/cluster/k8s/haku/mailbox/app/deployment.yaml")
    deployment = yaml.safe_load(deployment_path.read_text())

    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod_spec = deployment["spec"]["template"]["spec"]
    assert len(pod_spec["initContainers"]) == 1
    assert len(pod_spec["containers"]) == 1

    initialize = pod_spec["initContainers"][0]
    production = pod_spec["containers"][0]
    assert initialize["image"] == production["image"]
    assert initialize["command"] == ["/bin/sh", "/etc/stalwart/initialize.sh"]

    initialize_env = {item["name"] for item in initialize["env"]}
    production_env = {item["name"] for item in production["env"]}
    assert "STALWART_ADMIN_PASSWORD" in initialize_env
    assert "STALWART_ADMIN_PASSWORD" not in production_env
    assert "STALWART_RECOVERY_ADMIN" not in initialize_env | production_env

    assert initialize["securityContext"]["capabilities"] == {"add": ["NET_BIND_SERVICE"], "drop": ["ALL"]}
    assert production["securityContext"]["capabilities"] == {"drop": ["ALL"]}

    config_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "config")
    assert config_volume["configMap"]["name"] == "haku-mailbox-config"


if __name__ == "__main__":
    pytest_bazel.main()
