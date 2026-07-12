import json
from typing import Any

import pytest_bazel

from util.bazel.runfiles import get_required_path


def upsert(object_type: str, match_on: list[str], **values: Any) -> dict[str, Any]:
    return {"@type": "upsert", "object": object_type, "matchOn": match_on, "value": values}


def update(object_type: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"@type": "update", "object": object_type, "value": value}


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
        "Account",
        ["name"],
        **{
            "acct-reconciler": {
                "@type": "User",
                "name": "stalwart-reconciler",
                "domainId": "#dom",
                "roles": {"@type": "Admin"},
                "description": "Declarative plan reconciler (dedicated Authentik machine identity)",
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
    upsert(
        "NetworkListener",
        ["protocol"],
        **{
            "lst-smtp": {
                "name": "smtp",
                "protocol": "smtp",
                "bind": {"0.0.0.0:2525": True},
                "useTls": True,
                "tlsImplicit": False,
            },
            "lst-http": {"name": "http", "protocol": "http", "bind": {"0.0.0.0:8080": True}, "useTls": False},
            "lst-imap": {"name": "imap", "protocol": "imap", "bind": {"0.0.0.0:1143": True}, "useTls": False},
        },
    ),
    upsert("Role", ["description"], **{"role-user": {"description": "User"}}),
    upsert("Role", ["description"], **{"role-admin": {"description": "System Administrator"}}),
    update(
        "Authentication",
        {
            "directoryId": "#dir-authentik",
            "defaultUserRoleIds": {"#role-user": True},
            "defaultAdminRoleIds": {"#role-admin": True, "#role-user": True},
        },
    ),
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
    plan_path = get_required_path("ducktape/cluster/k8s/haku/mailbox/bootstrap/mailbox-plan.ndjson")
    actual = [json.loads(line) for line in plan_path.read_text().splitlines() if line.strip()]
    assert actual == EXPECTED_PLAN


if __name__ == "__main__":
    pytest_bazel.main()
