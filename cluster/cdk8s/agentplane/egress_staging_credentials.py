"""agentplane-staging's egress credentials: each real account the staging proxy presents for a
sandbox, with the Secret plumbing that delivers it and, beyond the GitHub PAT whose EgressCredential
and policy both environments share (`egress.py`), the EgressPolicy that scopes where it is
presented. Testing copies in only the GitHub PAT (`egress_testing_credentials.py`).
"""

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecSource,
    EgressCredentialSpecSourceSecretRef,
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicy,
    EgressPolicySpec,
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStore,
    ClusterSecretStoreSpec,
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecProviderKubernetes,
    ClusterSecretStoreSpecProviderKubernetesAuth,
    ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
    ClusterSecretStoreSpecProviderKubernetesServer,
    ClusterSecretStoreSpecProviderKubernetesServerCaProvider,
    ClusterSecretStoreSpecProviderKubernetesServerCaProviderType,
)

from cluster.cdk8s.agentplane.app_settings import FORGEJO_HAKU_POLICY, GOOGLE_READONLY_POLICY
from cluster.cdk8s.agentplane.egress import FORGEJO_HOST
from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_READER,
    EXTERNAL_CREDS_STORE,
    GITHUB_PAT_SECRET,
    credential_external_secret,
)
from cluster.cdk8s.metadata import metadata

_FORGEJO_STORE = "kubernetes-agentplane-staging-forgejo-secret-store"


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


def _forgejo_haku(scope: Construct, *, reader: ServiceAccount, namespace: str, credentials_namespace: str) -> None:
    source_role = Role(
        scope,
        "forgejo-source-role",
        metadata=metadata("agentplane-staging-egress-forgejo-reader", "haku-sandbox"),
        rules=[
            RolePolicyRule(
                resources=[Secret.from_secret_name(scope, "forgejo-source", "haku-forgejo-git")], verbs=["get"]
            )
        ],
    )
    RoleBinding(
        scope,
        "forgejo-source-binding",
        metadata=metadata("agentplane-staging-egress-forgejo-reader", "haku-sandbox"),
        role=source_role,
    ).add_subjects(reader)
    ClusterSecretStore(
        scope,
        "forgejo-store",
        metadata=ApiObjectMetadata(name=_FORGEJO_STORE),
        spec=ClusterSecretStoreSpec(
            conditions=[ClusterSecretStoreSpecConditions(namespaces=[credentials_namespace])],
            provider=ClusterSecretStoreSpecProvider(
                kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                    remote_namespace="haku-sandbox",
                    auth=ClusterSecretStoreSpecProviderKubernetesAuth(
                        service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
                            name=EXTERNAL_CREDS_READER
                        )
                    ),
                    server=ClusterSecretStoreSpecProviderKubernetesServer(
                        ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                            type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                            namespace="default",
                        )
                    ),
                )
            ),
        ),
    )
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target="haku-forgejo-git",
        source="haku-forgejo-git",
        key="password",
        store=_FORGEJO_STORE,
    )
    EgressCredential(
        scope,
        "egresscredential-forgejo-haku",
        metadata=ApiObjectMetadata(name="forgejo-haku", namespace=namespace),
        spec=EgressCredentialSpec(
            description=(
                "The password of the `haku` account on the internal Forgejo, the service user that "
                "owns haku-state and haku's mirrors. Requests carrying it act as that account with "
                "its full authority -- it is the account's own password, not a scoped token, so it "
                "reaches every repository haku can reach and the web UI besides. The proxy narrows "
                "nothing but the host: treat a sandbox bound to this as holding haku's Forgejo "
                "account."
            ),
            source=EgressCredentialSpecSource(
                secret_ref=EgressCredentialSpecSourceSecretRef(name="haku-forgejo-git", key="password")
            ),
            # Git over HTTP and Forgejo's REST API both authenticate with `Basic
            # base64(haku:<password>)`, so the placeholder travels as the password half. A client
            # sends the username itself; only the secret half is substituted here.
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD
                )
            ],
        ),
    )
    EgressPolicy(
        scope,
        "egresspolicy-forgejo-haku",
        metadata=ApiObjectMetadata(name=FORGEJO_HAKU_POLICY, namespace=namespace),
        spec=EgressPolicySpec(
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
            ]
        ),
    )


def _google_readonly(scope: Construct, *, namespace: str) -> None:
    # The Secret itself arrives by Airlock's ClusterExternalSecret
    # (cluster/k8s/agents/airlock/google-access-token-eso.yaml).
    EgressCredential(
        scope,
        "egresscredential-google-readonly",
        metadata=ApiObjectMetadata(name="google-readonly", namespace=namespace),
        spec=EgressCredentialSpec(
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
            source=EgressCredentialSpecSource(
                secret_ref=EgressCredentialSpecSourceSecretRef(name="google-access-token", key="access_token")
            ),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        ),
    )
    EgressPolicy(
        scope,
        "egresspolicy-google-readonly",
        metadata=ApiObjectMetadata(name=GOOGLE_READONLY_POLICY, namespace=namespace),
        spec=EgressPolicySpec(
            rules=[
                # One API per host, so read-only methods are the only restriction needed.
                EgressPolicySpecRules(
                    hosts=[
                        "gmail.googleapis.com",
                        "tasks.googleapis.com",
                        "people.googleapis.com",
                        "docs.googleapis.com",
                        "sheets.googleapis.com",
                        "slides.googleapis.com",
                        "youtube.googleapis.com",
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
            ]
        ),
    )
