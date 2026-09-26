"""agentplane-staging's egress credentials: each real account the staging proxy presents for a
sandbox, with the Secret plumbing that delivers it and, beyond the GitHub PAT whose EgressCredential
and policy both environments share (`egress.py`), the EgressPolicy that scopes where it is
presented. Testing copies in only the GitHub PAT (`egress_testing_credentials.py`).
"""

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.agentplane.app_settings import (
    ACTIVITYWATCH_READ_POLICY,
    AIQUOTA_READ_POLICY,
    FORGEJO_HAKU_POLICY,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
)
from cluster.cdk8s.agentplane.egress import FORGEJO_HOST, HOME_ASSISTANT_HOST
from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_READER,
    EXTERNAL_CREDS_STORE,
    GITHUB_PAT_SECRET,
    credential_external_secret,
)
from cluster.cdk8s.aiquota import AGENTPLANE_STAGING_BEARER
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.home_assistant.app import AGENTPLANE_READER_TOKEN
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy

# Written by tf/gitops/agent-machine-access/grocy-sf.tf into agents-infra, named after the Authentik
# service account whose app password it holds.
_GROCY_SF_ACCOUNT = "agentplane-grocy-sf-readonly"
# Minted for Authentik's `haku` service account and published into flux-system by the
# authentik-jwt-rotation CronJob (`haku-mail` entry); haku/mailbox.py mirrors the same Secret into
# haku-sandbox.
_HAKU_MAIL_TOKEN = "haku-mail-token"


def add_staging_egress_credentials(scope: Construct, *, namespace: str, credentials_namespace: str) -> None:
    construct = Construct(scope, "staging-egress-credentials")
    reader = ServiceAccount(construct, "reader", metadata=metadata(EXTERNAL_CREDS_READER, credentials_namespace))
    credential_external_secret(
        construct,
        namespace=credentials_namespace,
        target=GITHUB_PAT_SECRET,
        source="github-agentydragon-agent",
        key="token",
        store=EXTERNAL_CREDS_STORE,
    )
    _forgejo_haku(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _google_readonly(construct, namespace=namespace)
    _grocy_sf_readonly(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _home_assistant_readonly(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _activitywatch_read(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)
    _aiquota_read(construct, namespace=namespace)
    _haku_mailbox(construct, reader=reader, namespace=namespace, credentials_namespace=credentials_namespace)


def _forgejo_haku(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="haku-forgejo-git",
        source="haku-forgejo-git",
        key="password",
        store=single_secret_store(
            scope,
            "agentplane-staging-forgejo",
            reader=reader,
            source_namespace="haku-sandbox",
            source_secret="haku-forgejo-git",
            consumer_namespace=credentials_namespace,
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-forgejo-haku",
        metadata=ApiObjectMetadata(name="forgejo-haku", namespace=namespace),
        description=(
            "The password of the `haku` account on the internal Forgejo, the service user that "
            "owns haku-state and haku's mirrors. Requests carrying it act as that account with "
            "its full authority -- it is the account's own password, not a scoped token, so it "
            "reaches every repository haku can reach and the web UI besides. The proxy narrows "
            "nothing but the host: treat a sandbox bound to this as holding haku's Forgejo "
            "account."
        ),
        source=Source.secret_ref(name="haku-forgejo-git", key="password").to_spec(),
        # Git over HTTP and Forgejo's REST API both authenticate with `Basic
        # base64(haku:<password>)`, so the placeholder travels as the password half. A client
        # sends the username itself; only the secret half is substituted here.
        targets=[
            EgressCredentialSpecTargets(header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD)
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-forgejo-haku",
        metadata=ApiObjectMetadata(name=FORGEJO_HAKU_POLICY, namespace=namespace),
        rules=[
            # No method or path list. The credential is haku's whole account, so a verb or path
            # list here would narrow the request without narrowing the authority behind it --
            # the same reason the Kubernetes rule carries none. What it does admit is the whole
            # Forgejo surface: git smart-HTTP (clone, fetch and push), the REST API, and the
            # web UI.
            EgressPolicySpecRules(
                hosts=[FORGEJO_HOST],
                cluster_internal=True,
                credential_ref=EgressPolicySpecRulesCredentialRef(name="forgejo-haku"),
            )
        ],
    )


def _google_readonly(scope: Construct, *, namespace: str) -> None:
    # The Secret itself arrives by Airlock's ClusterExternalSecret
    # (cluster/cdk8s/airlock.py).
    EgressCredential(
        scope,
        "egresscredential-google-readonly",
        metadata=ApiObjectMetadata(name="google-readonly", namespace=namespace),
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
        source=Source.secret_ref(name="google-access-token", key="access_token").to_spec(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-google-readonly",
        metadata=ApiObjectMetadata(name=GOOGLE_READONLY_POLICY, namespace=namespace),
        rules=[
            # One API per host, so read-only methods are the only restriction needed.
            EgressPolicySpecRules(
                hosts=[
                    # keep-sorted start
                    "docs.googleapis.com",
                    "gmail.googleapis.com",
                    "people.googleapis.com",
                    "sheets.googleapis.com",
                    "slides.googleapis.com",
                    "tasks.googleapis.com",
                    "youtube.googleapis.com",
                    # keep-sorted end
                ],
                methods=[EgressPolicySpecRulesMethods.GET],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="google-readonly"),
            ),
            # www.googleapis.com serves many Google APIs, so paths pick the ones meant here.
            # YouTube is reachable on both hosts; client libraries differ on which they use.
            EgressPolicySpecRules(
                hosts=["www.googleapis.com"],
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/calendar/v3/**", "/drive/v3/**", "/youtube/v3/**"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="google-readonly"),
            ),
            # Drive Activity's only read is a POST query.
            EgressPolicySpecRules(
                hosts=["driveactivity.googleapis.com"],
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=["/v2/activity:query"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="google-readonly"),
            ),
        ],
    )


def _grocy_sf_readonly(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="grocy-sf-readonly",
        source=_GROCY_SF_ACCOUNT,
        key="password",
        store=single_secret_store(
            scope,
            "agentplane-staging-grocy-sf",
            reader=reader,
            source_namespace="agents-infra",
            source_secret=_GROCY_SF_ACCOUNT,
            consumer_namespace=credentials_namespace,
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-grocy-sf-readonly",
        metadata=ApiObjectMetadata(name="grocy-sf-readonly", namespace=namespace),
        description=(
            f"The app password of `{_GROCY_SF_ACCOUNT}`, an Authentik service account admitted to "
            "the grocy-sf application (tf/gitops/agent-machine-access/grocy-sf.tf), copied into "
            "this namespace by ESO. Send it as HTTP Basic under that username. Grocy knows the "
            "account as a user with no permissions, and `grocy-sf-readonly`'s rule presents the "
            "password only on GETs to Grocy's read routes."
        ),
        source=Source.secret_ref(name="grocy-sf-readonly", key="password").to_spec(),
        # The grocy-sf.allegedly.works outpost turns HTTP Basic into a client_credentials grant
        # against its own proxy provider, so the placeholder travels as the password half. A
        # client sends the username itself; only the password is substituted here.
        targets=[
            EgressCredentialSpecTargets(header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD)
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-grocy-sf-readonly",
        metadata=ApiObjectMetadata(name=GROCY_SF_READONLY_POLICY, namespace=namespace),
        rules=[
            # Talks to Grocy's own REST API (grocy-sf.allegedly.works/api/...) rather than
            # the grocy-mcp-sf MCP server: the MCP server's whole tool surface, reads and
            # writes alike, sits behind one POST /mcp JSON-RPC endpoint, which a host/method/
            # path rule cannot see inside to scope to reads only. Grocy's REST verbs express
            # that distinction directly, so GET-only admits exactly the read routes
            # (entities/stock/user/system/file); PUT, POST and DELETE -- every write -- are
            # refused by the proxy regardless of path.
            EgressPolicySpecRules(
                hosts=["grocy-sf.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.GET],
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
                credential_ref=EgressPolicySpecRulesCredentialRef(name="grocy-sf-readonly"),
            )
        ],
    )


def _home_assistant_readonly(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="home-assistant-readonly",
        source=AGENTPLANE_READER_TOKEN.secret_name,
        key="token",
        store=single_secret_store(
            scope,
            "agentplane-staging-home-assistant",
            reader=reader,
            source_namespace=AGENTPLANE_READER_TOKEN.secret_namespace,
            source_secret=AGENTPLANE_READER_TOKEN.secret_name,
            consumer_namespace=credentials_namespace,
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-home-assistant-readonly",
        metadata=ApiObjectMetadata(name="home-assistant-readonly", namespace=namespace),
        description=(
            "A long-lived token of `agentplane-reader`, a Home Assistant user in the read-only "
            "group, which the Home Assistant provisioner (homeassistant/provisioner) creates and "
            "keeps valid; ESO copies it into this namespace. Home Assistant refuses that user every service "
            "call, and `home-assistant-readonly`'s rule presents the token only on GETs of "
            "entity states and their history."
        ),
        source=Source.secret_ref(name="home-assistant-readonly", key="token").to_spec(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-home-assistant-readonly",
        metadata=ApiObjectMetadata(name=HOME_ASSISTANT_READONLY_POLICY, namespace=namespace),
        rules=[
            # A path rule sees no query string, so `/api/history/period/*` admits every entity's
            # history, not only the one a `filter_entity_id` names: the same reach as the
            # read-only group's, which covers every entity.
            EgressPolicySpecRules(
                hosts=[HOME_ASSISTANT_HOST],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/api/states", "/api/states/*", "/api/history/period/*"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="home-assistant-readonly"),
            )
        ],
    )


def _activitywatch_read(
    scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str
) -> None:
    # Exact source access: the activitywatch namespace also holds the write token.
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="activitywatch-read-token",
        source="activitywatch-read-token",
        key="token",
        store=single_secret_store(
            scope,
            "agentplane-staging-activitywatch",
            reader=reader,
            source_namespace="activitywatch",
            source_secret="activitywatch-read-token",
            consumer_namespace=credentials_namespace,
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-activitywatch-read",
        metadata=ApiObjectMetadata(name="activitywatch-read", namespace=namespace),
        description=(
            "The static bearer of the central ActivityWatch server's read route "
            "(cluster/docs/activitywatch/README.md), copied into this namespace by ESO. The "
            "route's own proxy admits it on GETs and on POST /api/0/query/ only, so it cannot "
            "write; what it reads is every device's window titles, URLs and AFK history."
        ),
        source=Source.secret_ref(name="activitywatch-read-token", key="token").to_spec(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-activitywatch-read",
        metadata=ApiObjectMetadata(name=ACTIVITYWATCH_READ_POLICY, namespace=namespace),
        rules=[
            # The API half of what the read route admits; its web UI stays unreachable. The query
            # endpoint needs its trailing slash: without it the route 301s, and a client following
            # that turns the POST into a GET.
            EgressPolicySpecRules(
                hosts=["activitywatch-read.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/api/0/**"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="activitywatch-read"),
            ),
            EgressPolicySpecRules(
                hosts=["activitywatch-read.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=["/api/0/query/"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="activitywatch-read"),
            ),
        ],
    )


def _aiquota_read(scope: Construct, *, namespace: str) -> None:
    # The Secret arrives by aiquota's own bearer mirror (aiquota.py), not a store here.
    EgressCredential(
        scope,
        "egresscredential-aiquota-read",
        metadata=ApiObjectMetadata(name="aiquota-read", namespace=namespace),
        description=(
            "aiquota's shared API bearer (aiquota.py), mirrored into this namespace by "
            "reflector. The API behind it serves only reads -- the Claude and Codex "
            "subscription quotas and each provider's raw usage response -- and "
            "`aiquota-read`'s rule presents it only on GETs under /v1/."
        ),
        source=Source.secret_ref(
            name=AGENTPLANE_STAGING_BEARER.secret_name, key=AGENTPLANE_STAGING_BEARER.secret_key_selector.key
        ).to_spec(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-aiquota-read",
        metadata=ApiObjectMetadata(name=AIQUOTA_READ_POLICY, namespace=namespace),
        rules=[
            EgressPolicySpecRules(
                hosts=["aiquota.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/v1/**"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="aiquota-read"),
            )
        ],
    )


def _haku_mailbox(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target=_HAKU_MAIL_TOKEN,
        source=_HAKU_MAIL_TOKEN,
        key="jwt",
        store=single_secret_store(
            scope,
            "agentplane-staging-haku-mail",
            reader=reader,
            source_namespace="flux-system",
            source_secret=_HAKU_MAIL_TOKEN,
            consumer_namespace=credentials_namespace,
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-haku-mailbox",
        metadata=ApiObjectMetadata(name="haku-mailbox", namespace=namespace),
        description=(
            "The Authentik JWT of Haku's mailbox account (haku@allegedly.works on the Stalwart "
            "server, cluster/k8s/haku/mailbox), rotated by authentik-jwt-rotation and copied "
            "into this namespace by ESO. It reads and changes the contents of that one mailbox "
            "over JMAP; it cannot send mail or administer the server (haku/docs/security.md)."
        ),
        source=Source.secret_ref(name=_HAKU_MAIL_TOKEN, key="jwt").to_spec(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-haku-mailbox",
        metadata=ApiObjectMetadata(name=HAKU_MAILBOX_POLICY, namespace=namespace),
        rules=[
            # JMAP only: the session document and the API, blob and event-source endpoints under
            # /jmap/. The same host serves Stalwart's management API, which stays unreachable.
            # JMAP's reads and writes share one POST endpoint, so no rule here can narrow it
            # to reads; the account itself is what bounds it.
            EgressPolicySpecRules(
                hosts=["haku-mailbox.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/.well-known/jmap", "/jmap/**"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="haku-mailbox"),
            ),
            EgressPolicySpecRules(
                hosts=["haku-mailbox.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=["/jmap/**"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="haku-mailbox"),
            ),
        ],
    )
