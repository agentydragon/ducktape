"""agentplane-staging's egress credentials: each real account the staging proxy presents for a
sandbox, with the Secret plumbing that delivers it and, beyond the GitHub PAT whose EgressCredential
and policy both environments share (`egress.py`), the egress PolicySet that scopes where it is
presented. Testing shares GitHub, BuildBuddy, and inference credentials (`egress_testing_credentials.py`).

Keep each public Gateway egress PolicySet in sync with the shared Cilium backend grants in
add_staging_egress_credentials: a Gateway on the proxy's own node rechecks the proxy's egress
against the selected backend's identity and target port. Node:443 alone completes TLS but returns HTTP 403 (#9495). Reuse the backend's
ServiceRef so selector/port changes propagate; the egress PolicySet still fences hosts, paths,
methods and credentials. See cluster/docs/cilium_network_policy.md, "Egress through a local Gateway".
"""

from collections.abc import Sequence

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.activitywatch import app as activitywatch
from cluster.cdk8s.agentplane.egress import FORGEJO_HOST, FORGEJO_HOST_ALIASES, FORGEJO_PUBLIC_HOST, HOME_ASSISTANT_HOST
from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_READER,
    EXTERNAL_CREDS_STORE,
    GITHUB_PAT_SECRET,
    credential_external_secret,
    inference_credentials,
)
from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.aiquota import AGENTPLANE_STAGING_BEARER, SERVICE as AIQUOTA_SERVICE
from cluster.cdk8s.authentik import app as authentik
from cluster.cdk8s.clickhouse import client as clickhouse
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.haku import mailbox
from cluster.cdk8s.home_assistant.app import AGENTPLANE_READER_TOKEN
from cluster.cdk8s.plaid_mcp import pgweb as plaid_pgweb
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.public_coder.egress import add_gateway_resources

# Written by tf/gitops/agent-machine-access/grocy-sf.tf into agents-infra, named after the Authentik
# service account whose app password it holds.
_GROCY_SF_ACCOUNT = "agentplane-grocy-sf-readonly"
# Minted for Authentik's `haku` service account and published into flux-system by the
# authentik-jwt-rotation CronJob (`haku-mail` entry); haku/mailbox.py mirrors the same Secret into
# haku-sandbox.
_HAKU_MAIL_TOKEN = "haku-mail-token"

# The staging-only egress PolicySets created below, each carrying a credential that exists
# only here. Exported because a launch preset or a PolicyBinding refers to them by name; as in
# egress.py, a name lives with the policy that answers to it.
FORGEJO_HAKU_POLICY = "forgejo-haku"
FORGEJO_FINANCE_AGENT_POLICY = "forgejo-finance-agent"
HAKU_MAILBOX_POLICY = "haku-mailbox"
GOOGLE_READONLY_POLICY = "google-readonly"
ACTIVITYWATCH_READ_POLICY = "activitywatch-read"
AIQUOTA_READ_POLICY = "aiquota-read"
FINANCE_AIQUOTA_HISTORY_POLICY = "finance-aiquota-history"
PLAID_PGWEB_POLICY = "plaid-pgweb"
# Credentials whose egress rules ride in the Action PolicySets that approve the same system's
# reads (actions_staging_policies.py), so a binding to one set grants both.
GROCY_SF_READONLY_CREDENTIAL = "grocy-sf-readonly"
HOME_ASSISTANT_READONLY_CREDENTIAL = "home-assistant-readonly"


def add_staging_egress_credentials(scope: Construct, *, namespace: str, credentials_namespace: str) -> None:
    construct = Construct(scope, "staging-egress-credentials")
    reader = ServiceAccount(
        construct, "reader", metadata=ApiObjectMetadata(name=EXTERNAL_CREDS_READER, namespace=credentials_namespace)
    )
    credential_external_secret(
        construct,
        namespace=credentials_namespace,
        target=GITHUB_PAT_SECRET,
        source="github-agentydragon-agent",
        key="token",
        store=EXTERNAL_CREDS_STORE,
    )
    _forgejo_haku(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _forgejo_finance_agent(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _google_readonly(construct, namespace=namespace)
    _grocy_sf_readonly(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _home_assistant_readonly(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _activitywatch_read(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _aiquota_read(construct, namespace=namespace)
    _finance_aiquota_history(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _haku_mailbox(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _buildbuddy(construct, namespace=namespace, credentials_namespace=credentials_namespace)
    _plaid_pgweb(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    inference_credentials(construct, reader=reader, credentials_namespace=credentials_namespace)
    add_gateway_resources(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    # Keep these grants in sync with the egress PolicySets in the named functions below.
    # Local Gateways check the selected backend's identity/target port, even for a public hostname.
    NetworkPolicy(
        construct,
        "networkpolicy-egress-staging-credentials",
        metadata=ApiObjectMetadata(name="agentplane-egress-staging-credentials", namespace=namespace),
        endpoint_selector=cilium.AGENTPLANE_STAGING_PROXY.selector,
        egress=[
            authentik.SERVER.egress(),  # _grocy_sf_readonly: the public route targets Authentik's outpost.
            activitywatch.READ.egress(),  # _activitywatch_read: bearer read sidecar.
            AIQUOTA_SERVICE.egress(),  # _aiquota_read: public Gateway and internal Service routes.
            clickhouse.HTTP.egress(),  # _finance_aiquota_history.
            mailbox.HTTP.egress(),  # _haku_mailbox: application policy excludes management APIs.
            plaid_pgweb.SERVICE.egress(),  # _plaid_pgweb.
        ],
    )


_BASIC_PASSWORD = EgressCredentialSpecTargets(
    header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD
)
_BEARER = EgressCredentialSpecTargets(
    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
)


def _copy_secret(
    scope: Construct,
    store: str,
    *,
    reader: ServiceAccount,
    credentials_namespace: str,
    source_namespace: str,
    source: str,
    key: str,
    target: str | None = None,
) -> None:
    """ESO copy of `source_namespace`/`source` into the credentials namespace as `target` (default:
    the same name), through a store that reads that one Secret."""
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target=target or source,
        source=source,
        key=key,
        store=single_secret_store(
            scope,
            f"agentplane-staging-{store}",
            reader=reader,
            source_namespace=source_namespace,
            source_secret=source,
            consumer_namespace=credentials_namespace,
        ),
    )


def _rule(
    credential: str,
    *hosts: str,
    method: PolicySetSpecRulesMethods | None = None,
    paths: Sequence[str] | None = None,
    cluster_internal: bool | None = None,
) -> PolicySetSpecRules:
    return Rule.egress(
        hosts=list(hosts),
        cluster_internal=cluster_internal,
        methods=None if method is None else [method],
        paths=None if paths is None else list(paths),
        credential_ref=PolicySetSpecRulesCredentialRef(name=credential),
    )


def _credential(
    scope: Construct,
    id: str,
    *,
    namespace: str,
    credential: str,
    description: str,
    secret: str,
    key: str,
    target: EgressCredentialSpecTargets,
) -> None:
    EgressCredential(
        scope,
        f"egresscredential-{id}",
        metadata=ApiObjectMetadata(name=credential, namespace=namespace),
        description=description,
        source=Source.secret_ref(name=secret, key=key),
        targets=[target],
    )


def _present(
    scope: Construct,
    policy: str,
    *,
    namespace: str,
    credential: str,
    description: str,
    secret: str,
    key: str,
    target: EgressCredentialSpecTargets,
    rules: Sequence[PolicySetSpecRules],
) -> None:
    """The EgressCredential `credential`, read from `secret`/`key`, and the egress PolicySet `policy`
    that scopes where the proxy presents it."""
    _credential(
        scope,
        policy,
        namespace=namespace,
        credential=credential,
        description=description,
        secret=secret,
        key=key,
        target=target,
    )
    add_egress_policy(
        scope, f"egresspolicy-{policy}", metadata=ApiObjectMetadata(name=policy, namespace=namespace), rules=rules
    )


def _forgejo_rules(credential: str) -> list[PolicySetSpecRules]:
    # No method or path list. The credential is a whole Forgejo account, so a verb or path list
    # here would narrow the request without narrowing the authority behind it -- the same reason
    # the Kubernetes rule carries none. What it does admit is the whole Forgejo surface: git
    # smart-HTTP (clone, fetch and push), the REST API, and the web UI. That holds under every
    # name: the host list decides which spellings reach the Service, not what the password may do.
    return [
        _rule(credential, FORGEJO_HOST, *FORGEJO_HOST_ALIASES, cluster_internal=True),
        # The public name is a rule of its own so it stays off `cluster_internal`: it resolves to
        # public addresses, and were it ever to resolve into the cluster the proxy's refusal of
        # private addresses should still stop the password going there.
        _rule(credential, FORGEJO_PUBLIC_HOST),
    ]


def _forgejo_haku(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    _copy_secret(
        scope,
        "forgejo",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace="haku-sandbox",
        source="haku-forgejo-git",
        key="password",
    )
    _present(
        scope,
        FORGEJO_HAKU_POLICY,
        namespace=namespace,
        credential="forgejo-haku",
        description=(
            "The password of the `haku` account on the internal Forgejo, the service user that "
            "owns haku-state and haku's mirrors. Requests carrying it act as that account with "
            "its full authority -- it is the account's own password, not a scoped token, so it "
            "reaches every repository haku can reach and the web UI besides. The proxy narrows "
            "nothing but the host: treat a sandbox bound to this as holding haku's Forgejo "
            "account."
        ),
        secret="haku-forgejo-git",
        key="password",
        # Git over HTTP and Forgejo's REST API both authenticate with `Basic
        # base64(haku:<password>)`, so the placeholder travels as the password half. A client
        # sends the username itself; only the secret half is substituted here.
        target=_BASIC_PASSWORD,
        rules=_forgejo_rules("forgejo-haku"),
    )


def _forgejo_finance_agent(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    # Sources directly from the `forgejo` namespace, where tf/gitops/finance-agent creates the
    # Secret -- unlike `_forgejo_haku` above, nothing else needs a copy of it, so there is no
    # existing namespace to piggyback on.
    _copy_secret(
        scope,
        "forgejo-finance-agent",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace="forgejo",
        source="finance-agent-git-creds",
        key="password",
    )
    _present(
        scope,
        FORGEJO_FINANCE_AGENT_POLICY,
        namespace=namespace,
        credential="forgejo-finance-agent",
        description=(
            "The password of the `finance-agent` account on the internal Forgejo, the service "
            "user that owns the finance-agent repo (tf/gitops/finance-agent) -- Rai's personal "
            "financial-leash agent workspace. Requests carrying it act as that account with its "
            "full authority, but the account owns exactly one repo, so the blast radius is that "
            "repo alone."
        ),
        secret="finance-agent-git-creds",
        key="password",
        # Same Basic-auth shape as forgejo-haku.
        target=_BASIC_PASSWORD,
        rules=_forgejo_rules("forgejo-finance-agent"),
    )


def _google_readonly(scope: Construct, *, namespace: str) -> None:
    # The Secret itself arrives by Airlock's ClusterExternalSecret
    # (cluster/cdk8s/airlock.py).
    credential = "google-readonly"
    _present(
        scope,
        GOOGLE_READONLY_POLICY,
        namespace=namespace,
        credential=credential,
        description=(
            "A Google OAuth access token minted and refreshed by Airlock "
            "(cluster/k8s/agents/airlock), mirrored into this namespace by ESO: the token of "
            "Airlock's `google` provider, whose scopes are all `.readonly` (Gmail, Calendar, "
            "Drive, Drive Activity, Tasks, Contacts, Docs, Sheets, Slides, YouTube). What the "
            "token was actually granted is whatever the operator consented to at Airlock, which "
            "can be narrower than that list -- an API outside it answers 403 "
            "`insufficientPermissions`. `google-readonly`'s rules restrict where the proxy "
            "presents it."
        ),
        secret="google-access-token",
        key="access_token",
        target=_BEARER,
        rules=[
            # One API per host, so read-only methods are the only restriction needed.
            _rule(
                credential,
                # keep-sorted start
                "docs.googleapis.com",
                "gmail.googleapis.com",
                "people.googleapis.com",
                "sheets.googleapis.com",
                "slides.googleapis.com",
                "tasks.googleapis.com",
                "youtube.googleapis.com",
                # keep-sorted end
                method=PolicySetSpecRulesMethods.GET,
            ),
            # www.googleapis.com serves many Google APIs, so paths pick the ones meant here.
            # YouTube is reachable on both hosts; client libraries differ on which they use.
            _rule(
                credential,
                "www.googleapis.com",
                method=PolicySetSpecRulesMethods.GET,
                paths=["/calendar/v3/**", "/drive/v3/**", "/youtube/v3/**"],
            ),
            # Drive Activity's only read is a POST query.
            _rule(
                credential,
                "driveactivity.googleapis.com",
                method=PolicySetSpecRulesMethods.POST,
                paths=["/v2/activity:query"],
            ),
        ],
    )


def _grocy_sf_readonly(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    credential = GROCY_SF_READONLY_CREDENTIAL
    _copy_secret(
        scope,
        "grocy-sf",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace="agents-infra",
        source=_GROCY_SF_ACCOUNT,
        key="password",
        target=credential,
    )
    _credential(
        scope,
        credential,
        namespace=namespace,
        credential=credential,
        description=(
            f"The app password of `{_GROCY_SF_ACCOUNT}`, an Authentik service account admitted to "
            "the grocy-sf application (tf/gitops/agent-machine-access/grocy-sf.tf), copied into "
            "this namespace by ESO. Send it as HTTP Basic under that username. Grocy knows the "
            "account as a user with no permissions, and the `grocy-sf-reads` PolicySet presents the "
            "password only on GETs to Grocy's read routes."
        ),
        secret=credential,
        key="password",
        # The grocy-sf.allegedly.works outpost turns HTTP Basic into a client_credentials grant
        # against its own proxy provider, so the placeholder travels as the password half. A
        # client sends the username itself; only the password is substituted here.
        target=_BASIC_PASSWORD,
    )


def grocy_sf_read_egress_rules() -> list[PolicySetSpecRules]:
    """Where the proxy presents `grocy-sf-readonly`; part of the `grocy-sf-reads` PolicySet."""
    return [
        # Talks to Grocy's own REST API (grocy-sf.allegedly.works/api/...) rather than
        # the grocy-mcp-sf MCP server: the MCP server's whole tool surface, reads and
        # writes alike, sits behind one POST /mcp JSON-RPC endpoint, which a host/method/
        # path rule cannot see inside to scope to reads only. Grocy's REST verbs express
        # that distinction directly, so GET-only admits exactly the read routes
        # (entities/stock/user/system/file); PUT, POST and DELETE -- every write -- are
        # refused by the proxy regardless of path.
        _rule(
            GROCY_SF_READONLY_CREDENTIAL,
            "grocy-sf.allegedly.works",
            method=PolicySetSpecRulesMethods.GET,
            paths=[
                # keep-sorted start
                "/api/files/**",
                "/api/objects/**",
                # `/api/stock/**` does not match `/api/stock` itself.
                "/api/stock",
                "/api/stock/**",
                "/api/system/db-changed-time",
                "/api/system/info",
                "/api/user",
                # keep-sorted end
            ],
        )
    ]


def _home_assistant_readonly(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    credential = HOME_ASSISTANT_READONLY_CREDENTIAL
    _copy_secret(
        scope,
        "home-assistant",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace=AGENTPLANE_READER_TOKEN.secret_namespace,
        source=AGENTPLANE_READER_TOKEN.secret_name,
        key="token",
        target=credential,
    )
    _credential(
        scope,
        credential,
        namespace=namespace,
        credential=credential,
        description=(
            "A long-lived token of `agentplane-reader`, a Home Assistant user in the read-only "
            "group, which the Home Assistant provisioner (homeassistant/provisioner) creates and "
            "keeps valid; ESO copies it into this namespace. Home Assistant refuses that user every service "
            "call, and the `home-assistant-reads` PolicySet presents the token only on GETs of "
            "entity states and their history."
        ),
        secret=credential,
        key="token",
        target=_BEARER,
    )


def home_assistant_read_egress_rules() -> list[PolicySetSpecRules]:
    """Where the proxy presents `home-assistant-readonly`; part of the `home-assistant-reads` PolicySet."""
    return [
        # A path rule sees no query string, so `/api/history/period/*` admits every entity's
        # history, not only the one a `filter_entity_id` names: the same reach as the
        # read-only group's, which covers every entity.
        _rule(
            HOME_ASSISTANT_READONLY_CREDENTIAL,
            HOME_ASSISTANT_HOST,
            cluster_internal=True,
            method=PolicySetSpecRulesMethods.GET,
            paths=["/api/states", "/api/states/*", "/api/history/period/*"],
        )
    ]


def _activitywatch_read(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    # Exact source access: the activitywatch namespace also holds the write token.
    _copy_secret(
        scope,
        "activitywatch",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace="activitywatch",
        source="activitywatch-read-token",
        key="token",
    )
    credential = "activitywatch-read"
    host = "activitywatch-read.allegedly.works"
    # Keep this policy in sync with its backend grant in add_staging_egress_credentials.
    _present(
        scope,
        ACTIVITYWATCH_READ_POLICY,
        namespace=namespace,
        credential=credential,
        description=(
            "The static bearer of the central ActivityWatch server's read route "
            "(cluster/docs/activitywatch/README.md), copied into this namespace by ESO. The "
            "route's own proxy admits it on GETs and on POST to its query endpoint (with or "
            "without the trailing slash) only, so it cannot write; what it reads is every "
            "device's window titles, URLs and AFK history."
        ),
        secret="activitywatch-read-token",
        key="token",
        target=_BEARER,
        rules=[
            # The API half of what the read route admits; its web UI stays unreachable.
            _rule(credential, host, method=PolicySetSpecRulesMethods.GET, paths=["/api/0/**"]),
            # Both spellings of the query endpoint, as the route answers both (its anchored block in
            # cluster/k8s/activitywatch/bearer-proxy.conf.template): a spelling this fence refuses
            # dies with `403 denied; reason=no-rule` before the route could redirect it. Two paths
            # rather than one glob: `*` does not cross a `/` and `/api/0/query/**` does not match the
            # bare `/api/0/query`, so no single pattern covers both without also admitting
            # `/api/0/queryanything`.
            _rule(credential, host, method=PolicySetSpecRulesMethods.POST, paths=["/api/0/query", "/api/0/query/"]),
        ],
    )


def _aiquota_read(scope: Construct, *, namespace: str) -> None:
    # The Secret arrives by aiquota's own bearer mirror (aiquota.py), not a store here.
    credential = "aiquota-read"
    # Keep this policy in sync with its backend grant in add_staging_egress_credentials.
    _present(
        scope,
        AIQUOTA_READ_POLICY,
        namespace=namespace,
        credential=credential,
        description=(
            "aiquota's shared API bearer (aiquota.py), mirrored into this namespace by "
            "reflector. The API behind it serves only reads -- the Claude and Codex "
            "subscription quotas and each provider's raw usage response -- and "
            "`aiquota-read`'s rule presents it only on GETs under /v1/."
        ),
        secret=AGENTPLANE_STAGING_BEARER.secret_key.secret.name,
        key=AGENTPLANE_STAGING_BEARER.secret_key.key,
        target=_BEARER,
        rules=[
            _rule(credential, "aiquota.allegedly.works", method=PolicySetSpecRulesMethods.GET, paths=["/v1/**"]),
            # A second route over the cluster Service, beside the public one, because the public
            # Gateway leg intermittently loses connections to this API's pod in the other region -- a
            # flat-5.0 s `503 ... connection timeout`. Bursty, not a steady rate: 3 in its first 40
            # reads today, none in the 180 after. Budget a couple of retries; trust no fixed rate.
            # Two spelling traps, both re-verified 2026-10-09: the rule matches the host string
            # exactly, so the short `aiquota-api.cli-proxy-api` is refused `no-rule`; and the bare
            # FQDN dials port 80, which this backend's Cilium grant (8080 only) does not admit, so it
            # blackholes into a client timeout rather than refusing. Spell it
            # `aiquota-api.cli-proxy-api.svc.cluster.local:8080`.
            _rule(
                credential,
                AIQUOTA_SERVICE.fqdn,
                cluster_internal=True,
                method=PolicySetSpecRulesMethods.GET,
                paths=["/v1/quotas", "/v1/providers/*/raw"],
            ),
        ],
    )


def _finance_aiquota_history(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    name = clickhouse.FINANCE_AGENT_CREDENTIALS
    _copy_secret(
        scope,
        "finance-clickhouse",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace=clickhouse.NAMESPACE,
        source=name,
        key=clickhouse.PASSWORD_KEY,
    )
    # Keep this policy in sync with its backend grant in add_staging_egress_credentials.
    _present(
        scope,
        FINANCE_AIQUOTA_HISTORY_POLICY,
        namespace=namespace,
        credential=name,
        description="Finance-only typed AIQuota ClickHouse reader; HTTP Basic password for finance_agent_aiquota.",
        secret=name,
        key=clickhouse.PASSWORD_KEY,
        target=_BASIC_PASSWORD,
        rules=[
            _rule(name, clickhouse.HTTP.fqdn, cluster_internal=True, method=PolicySetSpecRulesMethods.GET, paths=["/"])
        ],
    )


def _haku_mailbox(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    _copy_secret(
        scope,
        "haku-mail",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace="flux-system",
        source=_HAKU_MAIL_TOKEN,
        key="jwt",
    )
    credential = "haku-mailbox"
    host = "haku-mailbox.allegedly.works"
    # Keep this policy in sync with its backend grant in add_staging_egress_credentials.
    _present(
        scope,
        HAKU_MAILBOX_POLICY,
        namespace=namespace,
        credential=credential,
        description=(
            "The Authentik JWT of Haku's mailbox account (haku@allegedly.works on the Stalwart "
            "server, cluster/k8s/haku/mailbox), rotated by authentik-jwt-rotation and copied "
            "into this namespace by ESO. It reads and changes the contents of that one mailbox "
            "over JMAP; it cannot send mail or administer the server (haku/docs/security.md)."
        ),
        secret=_HAKU_MAIL_TOKEN,
        key="jwt",
        target=_BEARER,
        rules=[
            # JMAP only: the session document and the API, blob and event-source endpoints under
            # /jmap/. The same host serves Stalwart's management API, which stays unreachable.
            # JMAP's reads and writes share one POST endpoint, so no rule here can narrow it
            # to reads; the account itself is what bounds it.
            _rule(credential, host, method=PolicySetSpecRulesMethods.GET, paths=["/.well-known/jmap", "/jmap/**"]),
            _rule(credential, host, method=PolicySetSpecRulesMethods.POST, paths=["/jmap/**"]),
        ],
    )


def _buildbuddy(scope: Construct, *, namespace: str, credentials_namespace: str) -> None:
    # The shared BuildBuddy API key lives in external-creds; this environment's ExternalSecret
    # copies it into the isolated namespace the shared egress credential reads.
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="buildbuddy-api-key",
        source="buildbuddy-api-key",
        key="api-key",
        store=EXTERNAL_CREDS_STORE,
    )


def _plaid_pgweb(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    _copy_secret(
        scope,
        "plaid-pgweb",
        reader=reader,
        credentials_namespace=credentials_namespace,
        source_namespace=plaid_pgweb.AUTH.namespace,
        source=plaid_pgweb.AUTH.name,
        key=plaid_pgweb.AUTH_KEY,
    )
    credential = "plaid-pgweb"
    host = plaid_pgweb.SERVICE.fqdn
    # pgweb's other API routes and its web UI stay unreachable. Its session routes (`connect`,
    # `disconnect`, `switchdb`) are POSTs, so POST is admitted on the query route alone.
    reads = [
        # keep-sorted start
        "/api/connection",
        "/api/info",
        "/api/objects",
        "/api/query",
        "/api/schemas",
        "/api/tables/**",
        # keep-sorted end
    ]
    # Keep this policy in sync with its backend grant in add_staging_egress_credentials.
    _present(
        scope,
        PLAID_PGWEB_POLICY,
        namespace=namespace,
        credential=credential,
        description=(
            f"The HTTP Basic password of pgweb's `{plaid_pgweb.AUTH_USER}` user "
            "(cluster/cdk8s/plaid_mcp/pgweb.py), minted in plaid-mcp and copied into this namespace by "
            "ESO. Send it as HTTP Basic under that username. pgweb runs SQL as `plaid_ro`, a Postgres "
            "role that can only SELECT from the Plaid mirror -- every linked account's balances, "
            "transactions and holdings -- and `plaid-pgweb`'s rules present the password only on pgweb's "
            "query API."
        ),
        secret=plaid_pgweb.AUTH.name,
        key=plaid_pgweb.AUTH_KEY,
        target=_BASIC_PASSWORD,
        rules=[
            _rule(credential, host, cluster_internal=True, method=PolicySetSpecRulesMethods.GET, paths=reads),
            _rule(credential, host, cluster_internal=True, method=PolicySetSpecRulesMethods.POST, paths=["/api/query"]),
        ],
    )
