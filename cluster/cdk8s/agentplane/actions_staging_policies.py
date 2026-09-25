"""Staging-only Action Service policy objects: the claude-ai caller ServiceAccount, its
five reviewed GitHub-reads ActionPolicySets, its reviewed Home Assistant/Gmail/Google
Calendar-reads ActionPolicySets, and the ActionPolicyBinding granting them to that
ServiceAccount; also what its sandboxes may reach (the EgressBinding) and read (the Coinbase
key). See cluster/k8s/agentplane-staging/README.md § Action policies -- Sandbox-subject
bindings are written by the integration app at runtime and are never checked in here.
"""

from __future__ import annotations

from agentplane_actionpolicybinding_crds.works.allegedly.agentplane import (
    ActionPolicyBinding,
    ActionPolicyBindingSpec,
    ActionPolicyBindingSpecSubject,
)
from agentplane_actionpolicyset_crds.works.allegedly.agentplane import (
    ActionPolicySet,
    ActionPolicySetSpec,
    ActionPolicySetSpecAutoApproveIf,
    ActionPolicySetSpecAutoApproveIfType,
)
from agentplane_egressbinding_crds.works.allegedly.agentplane import (
    EgressBinding,
    EgressBindingSpec,
    EgressBindingSpecSubjects,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicy,
    EgressPolicySpec,
    EgressPolicySpecRules,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from agentplane.action_service.policies.resources import BindingSpec, PolicySetSpec
from agentplane.action_service.sandbox_executor import SANDBOX_GROUP, SandboxAction
from cluster.cdk8s import cilium, external_creds
from cluster.cdk8s.agentplane import app as app_component, egress, testing
from cluster.cdk8s.agentplane.app_settings import (
    BASIC_POLICY,
    COINBASE_POLICY,
    FORGEJO_HAKU_POLICY,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
    KUBERNETES_POLICY,
    PACKAGES_POLICY,
)
from cluster.cdk8s.agentplane.staging_config import (
    PUBLIC_DUCKTAPE_FORK_READS_SET,
    PUBLIC_DUCKTAPE_READS_SET,
    PUBLIC_GAFFER_PRIVATE_READS_SET,
    PUBLIC_GITHUB_READS_SET,
)
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

_NAMESPACE = "agentplane-staging"
_GITHUB_READS_SET = "github-reads"
_SANDBOX_SET = "sandbox-self"
_GITHUB_IDENTITY_READS_SET = "github-identity-reads"
_HOME_ASSISTANT_READS_SET = "home-assistant-reads"
_GMAIL_READS_SET = "gmail-reads"
_GOOGLE_CALENDAR_READS_SET = "google-calendar-reads"
_TANA_READS_SET = "tana-reads"
_GROCY_SF_READS_SET = "grocy-sf-reads"
# The `cluster-sops-read` Coinbase CDP key, which can only view (no trade, no transfer): the one
# Haku's sandbox reads too. cluster/cdk8s/external_creds.py approves this namespace's copy.
_COINBASE_SECRET = "coinbase-api-credentials"
_AGENTPLANE_TESTING_POLICY = "agentplane-testing"
_GITHUB_DOWNLOADS_POLICY = "github-downloads"


def _policy_set(scope: Construct, id: str, *, metadata: ApiObjectMetadata, spec: ActionPolicySetSpec) -> None:
    # The Action Service parses `spec` more strictly than the CRD schema (an unknown policy
    # kind or key, an invalid JSON Schema); an object it refuses reports Ready=False on the
    # cluster and contributes nothing, so it fails here instead.
    PolicySetSpec.model_validate(ActionPolicySet(scope, id, metadata=metadata, spec=spec).to_json()["spec"])


def _binding(scope: Construct, id: str, *, metadata: ApiObjectMetadata, spec: ActionPolicyBindingSpec) -> None:
    BindingSpec.model_validate(ActionPolicyBinding(scope, id, metadata=metadata, spec=spec).to_json()["spec"])


# GitHub MCP's normal endpoint exposes its default catalog, including writes. This is the
# explicit 2026-08-14 upstream read-only subset: new upstream tools intentionally stay
# manual until reviewed here. `ui_get` reads an MCP App UI resource, not GitHub
# repository state.
_GITHUB_READS_ACTIONS = [
    "get_me",
    "get_team_members",
    "get_teams",
    "ui_get",
    "find_duplicate",
    "get_label",
    "issue_dependency_read",
    "issue_read",
    "list_issue_fields",
    "list_issue_types",
    "list_issues",
    "search_issues",
    "list_pull_requests",
    "pull_request_read",
    "search_pull_requests",
    "get_commit",
    "get_file_blame",
    "get_file_contents",
    "get_latest_release",
    "get_release_by_tag",
    "get_tag",
    "list_branches",
    "list_commits",
    "list_releases",
    "list_repository_collaborators",
    "list_tags",
    "search_code",
    "search_commits",
    "search_repositories",
    "search_users",
]

# The tool list every repository-scoped read set shares -- only the trusted owner/repo
# (or, for public-github-reads, a live visibility check) differs.
_REPOSITORY_SCOPED_ACTIONS = [
    "actions_get",
    "actions_list",
    "find_duplicate",
    "get_commit",
    "get_file_blame",
    "get_file_contents",
    "get_job_logs",
    "get_label",
    "get_latest_release",
    "get_release_by_tag",
    "get_tag",
    "issue_dependency_read",
    "issue_read",
    "list_branches",
    "list_commits",
    "list_issue_fields",
    "list_issue_types",
    "list_issues",
    "list_pull_requests",
    "list_releases",
    "list_repository_collaborators",
    "list_tags",
    "pull_request_read",
    "search_issues",
    "search_pull_requests",
    "search_code",
]


# Home Assistant's read-only surface: every `ha_get_*`/`ha_list_*`/`ha_config_get_*`/
# `ha_config_list_*` tool, plus `ha_search` -- none of which mutate state. Reviewed
# exclusions: `ha_get_camera_image` returns a live camera snapshot rather than reading
# state HA already holds, so it stays manual pending a privacy review. TODO(ha-camera):
# reconsider adding it once that review happens. `ha_eval_template` renders a Jinja
# template with the same context Automations get (entity/state access plus template
# functions), wide enough to stay manual. `ha_report_issue` files an external
# support/issue-tracker report, not a Home Assistant state read. New upstream tools stay
# manual until reviewed here, same convention as the GitHub reads set above.
_HOME_ASSISTANT_READS_ACTIONS = [
    "ha_config_get_automation",
    "ha_config_get_calendar_events",
    "ha_config_get_category",
    "ha_config_get_dashboard",
    "ha_config_get_label",
    "ha_config_get_scene",
    "ha_config_get_script",
    "ha_config_list_dashboard_resources",
    "ha_config_list_groups",
    "ha_config_list_helpers",
    "ha_get_app",
    "ha_get_automation_traces",
    "ha_get_blueprint",
    "ha_get_device",
    "ha_get_entity",
    "ha_get_entity_exposure",
    "ha_get_hacs_info",
    "ha_get_history",
    "ha_get_integration",
    "ha_get_logs",
    "ha_get_operation_status",
    "ha_get_overview",
    "ha_get_skill_guide",
    "ha_get_state",
    "ha_get_system_health",
    "ha_get_todo",
    "ha_get_zone",
    "ha_list_floors_areas",
    "ha_list_services",
    "ha_search",
]

# Gmail's generated read-only surface (haku/console/tools/gmail.py's _GMAIL_READ_TOOLS); writes
# (drafts_create/update/delete, threads_modify_labels, labels_create/patch/delete,
# filters_create/delete) stay on the human path.
_GMAIL_READS_ACTIONS = [
    "drafts_get",
    "drafts_list",
    "filters_get",
    "filters_list",
    "labels_get",
    "labels_list",
    "messages_get",
    "threads_get",
    "threads_list",
]

# Google Calendar's read-only surface (haku/console/tools/google_calendar.py); create_event
# stays on the human path.
_GOOGLE_CALENDAR_READS_ACTIONS = ["get_event", "list_event_instances", "list_events"]

# Tana's read-only surface, plus `get_or_create_calendar_node`, whose only write is creating a
# date's calendar node when it is missing. Reviewed exclusion: `open_node` navigates the
# operator's Tana desktop app. New upstream tools stay manual until reviewed here.
_TANA_READS_ACTIONS = [
    "get_children",
    "get_or_create_calendar_node",
    "get_tag_schema",
    "list_tags",
    "list_workspaces",
    "read_node",
    "search_nodes",
]

# Grocy SF's read-only surface, as haku-console's `grocy_reads` policy auto-approved it.
# Reviewed exclusion: `open_product_stock` marks a product opened. New upstream tools stay manual
# until reviewed here.
_GROCY_SF_READS_ACTIONS = [
    "entities_get",
    "entities_list",
    "file_get",
    "get_below_minimum_stock",
    "get_current_user",
    "get_db_changed_time",
    "get_expired_stock",
    "get_expiring_stock",
    "get_product_stock",
    "get_system_info",
    "list_volatile_stock",
    "locations_list",
    "product_groups_list",
    "products_list",
    "quantity_units_list",
    "shopping_list_get",
    "shopping_lists_list",
    "stock_entries_list",
    "stock_get",
]


def _repository_reads(scope: Construct, id: str, *, name: str, description: str, owner: str, repository: str) -> None:
    _policy_set(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE, annotations={"description": description}),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_REPOSITORY,
                    owner=owner,
                    repository=repository,
                    actions={"github": _REPOSITORY_SCOPED_ACTIONS},
                )
            ]
        ),
    )


def add_staging_action_policies(scope: Construct) -> None:
    # The principal a Connection enrolled from the Claude.ai MCP connector acts as, picked by
    # the operator at OAuth consent, and the identity its sandboxes run as. The label is what
    # makes it an Action caller, and removing the label or the object is how it is disabled.
    # What it may do without an operator is an ActionPolicyBinding naming it; its Kubernetes
    # access is listed in cluster/docs/agent_rbac.md § 6.
    claude_ai = ServiceAccount(
        scope,
        "serviceaccount-claude-ai",
        metadata=ApiObjectMetadata(
            name="claude-ai",
            namespace=_NAMESPACE,
            labels={"agentplane.allegedly.works/use-action-service": "true"},
            annotations={
                "description": "The principal for Connections enrolled from the Claude.ai MCP connector, selected at OAuth consent."
            },
        ),
        automount_token=False,
    )

    _policy_set(
        scope,
        "actionpolicyset-github-reads",
        metadata=ApiObjectMetadata(
            name=_GITHUB_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of GitHub MCP's default catalog; every other GitHub Action stays on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"github": _GITHUB_READS_ACTIONS},
                )
            ]
        ),
    )
    # `get_me` returns only the authenticated caller's own GitHub identity and has no
    # repository or mutation surface. Kept separate so a caller can be granted the
    # identity read without widening repository-scoped GitHub sets.
    _policy_set(
        scope,
        "actionpolicyset-github-identity-reads",
        metadata=ApiObjectMetadata(
            name=_GITHUB_IDENTITY_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The caller's own GitHub identity read, with no repository or mutation surface."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS, actions={"github": ["get_me"]}
                )
            ]
        ),
    )
    # The coder Agent's own fork, used to stage branches before opening PRs into
    # agentydragon/ducktape. It already has write access here (that's how it opens
    # PRs), so a read grant on its own fork's content adds no exposure beyond what it
    # can already write.
    _repository_reads(
        scope,
        "actionpolicyset-public-ducktape-fork-reads",
        name=PUBLIC_DUCKTAPE_FORK_READS_SET,
        description="Reviewed GitHub reads scoped to agentydragon-agent/ducktape, the coder Agent's fork.",
        owner="agentydragon-agent",
        repository="ducktape",
    )
    _repository_reads(
        scope,
        "actionpolicyset-public-ducktape-reads",
        name=PUBLIC_DUCKTAPE_READS_SET,
        description="Reviewed GitHub reads scoped to agentydragon/ducktape.",
        owner="agentydragon",
        repository="ducktape",
    )
    _repository_reads(
        scope,
        "actionpolicyset-public-gaffer-private-reads",
        name=PUBLIC_GAFFER_PRIVATE_READS_SET,
        description="Reviewed GitHub reads scoped to the private agentydragon/gaffer-private.",
        owner="agentydragon",
        repository="gaffer-private",
    )
    # Any repository confirmed genuinely public -- not bounded to a fixed owner/repo
    # allowlist. `github_public_repository` positively confirms the target
    # repository's visibility with a live, unauthenticated GitHub API call before
    # approving, rather than inferring "public" from the absence of a restriction.
    _policy_set(
        scope,
        "actionpolicyset-public-github-reads",
        metadata=ApiObjectMetadata(
            name=PUBLIC_GITHUB_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "Reviewed GitHub reads of any repository a live unauthenticated lookup confirms public."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_PUBLIC_UNDERSCORE_REPOSITORY,
                    actions={"github": _REPOSITORY_SCOPED_ACTIONS},
                )
            ]
        ),
    )

    # Coinbase authenticates each request with a fresh JWT signed over its method, host and path,
    # which the egress proxy's placeholder substitution cannot produce. So a sandbox of claude-ai's
    # holds the key and signs for itself: it may read this one Secret through the API server, and
    # the `coinbase` policy passes its GETs to api.coinbase.com unchanged. What makes handing the
    # sandbox the key acceptable is that the key can only view.
    ExternalSecret(
        scope,
        "coinbase-external-secret",
        name=_COINBASE_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=external_creds.STORE,
        data=[remote_data(_COINBASE_SECRET, key) for key in ("api_key", "api_secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        annotations={
            "description": "ESO copy of the view-only Coinbase CDP key from external-creds, read by claude-ai's sandboxes."
        },
    )
    coinbase_reader = Role(
        scope,
        "role-claude-ai-coinbase",
        metadata=metadata("claude-ai-coinbase-reader", _NAMESPACE),
        rules=[
            RolePolicyRule(
                resources=[Secret.from_secret_name(scope, "coinbase-secret", _COINBASE_SECRET)], verbs=["get"]
            )
        ],
    )
    RoleBinding(
        scope,
        "rolebinding-claude-ai-coinbase",
        metadata=metadata("claude-ai-coinbase-reader", _NAMESPACE),
        role=coinbase_reader,
    ).add_subjects(claude_ai)
    EgressPolicy(
        scope,
        "egresspolicy-coinbase",
        metadata=ApiObjectMetadata(name=COINBASE_POLICY, namespace=_NAMESPACE),
        spec=EgressPolicySpec(
            rules=[EgressPolicySpecRules(hosts=["api.coinbase.com"], methods=[EgressPolicySpecRulesMethods.GET])]
        ),
    )
    # The testing deployment's app, for the acceptance suite's harness scenarios run from a box
    # (agentplane/acceptance/README.md): by its Service rather than its public name, which would
    # hairpin out through the Gateway and back. Plain HTTP, so the proxy reads the request without
    # bumping TLS. Nothing is substituted: the suite presents the app token it mints in
    # agentplane-testing (rbac.AcceptanceToken), so any method may pass.
    EgressPolicy(
        scope,
        "egresspolicy-agentplane-testing",
        metadata=ApiObjectMetadata(name=_AGENTPLANE_TESTING_POLICY, namespace=_NAMESPACE),
        spec=EgressPolicySpec(
            rules=[
                EgressPolicySpecRules(
                    hosts=[f"{app_component.NAME}.{testing.ENV.namespace}.svc.cluster.local"], cluster_internal=True
                )
            ]
        ),
    )
    cilium.network_policy(
        scope,
        "networkpolicy-egress-to-testing-app",
        metadata=metadata(f"{egress.NAME}-to-testing-app", _NAMESPACE),
        selector={"app.kubernetes.io/name": egress.NAME},
        egress=[
            cilium.egress_to(
                cilium.endpoint_labels(testing.ENV.namespace, app_component.NAME), app_component.CONTAINER_PORT
            )
        ],
    )
    # GitHub downloads with nothing substituted: a release asset or a tag archive, which is what a
    # Bazel `http_archive` fetches, without the write-capable PAT `github-public` carries.
    EgressPolicy(
        scope,
        "egresspolicy-github-downloads",
        metadata=ApiObjectMetadata(name=_GITHUB_DOWNLOADS_POLICY, namespace=_NAMESPACE),
        spec=EgressPolicySpec(
            rules=[
                EgressPolicySpecRules(
                    hosts=[
                        "github.com",
                        # Where `github.com/.../archive/...` redirects.
                        "codeload.github.com",
                        # Where `github.com/.../releases/download/...` redirects.
                        "objects.githubusercontent.com",
                        "release-assets.githubusercontent.com",
                    ],
                    methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.HEAD],
                )
            ]
        ),
    )

    # What a sandbox of claude-ai's may reach. The binding is on the account rather than on each
    # box because that is what the account is entitled to: the proxy authenticates the Pod's
    # ServiceAccount, and every sandbox stamped for this caller runs as exactly that. Nothing else
    # runs as it -- claude-ai has no Pod of its own -- so this grants reach to its sandboxes and to
    # nothing else.
    #
    # `forgejo-haku` is the widest of these by some distance: it substitutes the `haku` account's
    # own Forgejo password, so a sandbox of this caller's acts as haku across every repository that
    # account owns. It is here because the operator asked for it; it is not a default any caller
    # should inherit. `packages` is the opposite end: public mirrors, no credential, GET and HEAD.
    # `google-readonly` substitutes Airlock's read-only Google token on Gmail, Calendar, Drive,
    # Drive Activity, Tasks, Contacts, Docs, Sheets, Slides and YouTube reads
    # (egress_staging_credentials.py). `grocy-sf-readonly` presents the app password of an Authentik
    # service account with no Grocy permissions, as HTTP Basic, on GETs to Grocy's own REST API (see
    # that module's `grocy-sf-readonly` EgressPolicy for why that's Grocy's API rather than the
    # grocy-mcp-sf MCP server). `coinbase` presents nothing: the sandbox signs with the key above.
    # `agentplane-testing` presents nothing either: the acceptance suite brings its own app token.
    # `github-downloads` presents nothing either: public GitHub downloads, GET and HEAD only.
    #
    # TODO(github-egress): consider binding `github-public` here too. The ActionPolicyBinding below
    # auto-approves GitHub *reads through the Action Service*, while a sandbox of the same caller
    # has only `github-downloads`: GET and HEAD with no credential, so `git clone`, whose fetch
    # POSTs to `git-upload-pack`, fails for a repository its caller can read through an Action.
    # What stands in the way is `github-public`'s credential: the `agentydragon-agent` PAT is
    # write-capable and substituted on GET and POST with no path limit, so binding it lets a
    # sandbox push as that bot.
    EgressBinding(
        scope,
        "egressbinding-claude-ai",
        metadata=ApiObjectMetadata(
            name="claude-ai",
            namespace=_NAMESPACE,
            annotations={"description": "What sandboxes running as the claude-ai ServiceAccount may reach."},
        ),
        spec=EgressBindingSpec(
            subjects=[EgressBindingSpecSubjects(namespace=_NAMESPACE, name="claude-ai")],
            policies=[
                BASIC_POLICY,
                KUBERNETES_POLICY,
                FORGEJO_HAKU_POLICY,
                PACKAGES_POLICY,
                GOOGLE_READONLY_POLICY,
                GROCY_SF_READONLY_POLICY,
                HOME_ASSISTANT_READONLY_POLICY,
                COINBASE_POLICY,
                _AGENTPLANE_TESTING_POLICY,
                _GITHUB_DOWNLOADS_POLICY,
            ],
        ),
    )

    # Every sandbox Action, auto-approved. Approving each one individually would not be a
    # boundary: a sandbox runs as the account that asked for it, so the whole group can do
    # exactly what that account can already do, and `exec` inside it is not narrower than
    # `provision` of it. What decides whether this is safe for an account is that account's own
    # egress and Kubernetes authority, which is why this set is bound per caller and not by default.
    _policy_set(
        scope,
        "actionpolicyset-sandbox-self",
        metadata=ApiObjectMetadata(
            name=_SANDBOX_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "Auto-approves sandbox lifecycle and exec for a caller, which act only as that caller."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={SANDBOX_GROUP: sorted(SandboxAction)},
                )
            ]
        ),
    )

    # Home Assistant's read-only surface (see _HOME_ASSISTANT_READS_ACTIONS above for the
    # reviewed tool list and exclusions).
    _policy_set(
        scope,
        "actionpolicyset-home-assistant-reads",
        metadata=ApiObjectMetadata(
            name=_HOME_ASSISTANT_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of Home Assistant MCP's default catalog; every other Home Assistant Action stays on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"home_assistant": _HOME_ASSISTANT_READS_ACTIONS},
                )
            ]
        ),
    )

    # Gmail's and Google Calendar's read-only surfaces (see the _GMAIL_READS_ACTIONS /
    # _GOOGLE_CALENDAR_READS_ACTIONS lists above for the reviewed tool lists and exclusions).
    _policy_set(
        scope,
        "actionpolicyset-gmail-reads",
        metadata=ApiObjectMetadata(
            name=_GMAIL_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Gmail MCP backend's catalog; every write stays on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"gmail": _GMAIL_READS_ACTIONS},
                )
            ]
        ),
    )
    _policy_set(
        scope,
        "actionpolicyset-google-calendar-reads",
        metadata=ApiObjectMetadata(
            name=_GOOGLE_CALENDAR_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Google Calendar MCP backend's catalog; create_event stays on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"google_calendar": _GOOGLE_CALENDAR_READS_ACTIONS},
                )
            ]
        ),
    )

    _policy_set(
        scope,
        "actionpolicyset-tana-reads",
        metadata=ApiObjectMetadata(
            name=_TANA_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Tana MCP backend's catalog, plus get_or_create_calendar_node; every other write and open_node stay on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"tana": _TANA_READS_ACTIONS},
                )
            ]
        ),
    )
    _policy_set(
        scope,
        "actionpolicyset-grocy-sf-reads",
        metadata=ApiObjectMetadata(
            name=_GROCY_SF_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Grocy SF MCP backend's catalog; every write, open_product_stock included, stays on the human path."
            },
        ),
        spec=ActionPolicySetSpec(
            auto_approve_if=[
                ActionPolicySetSpecAutoApproveIf(
                    type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS,
                    actions={"grocy_sf": _GROCY_SF_READS_ACTIONS},
                )
            ]
        ),
    )

    # The reviewed read sets for GitHub (github-reads and github-identity-reads), Home
    # Assistant, Gmail, Google Calendar, Tana and Grocy SF, plus sandbox use, attached to the
    # Claude.ai connector's principal.
    # The binding's existence is the grant: deleting it, or the label on the ServiceAccount,
    # puts every one of these Actions back on the human path.
    _binding(
        scope,
        "actionpolicybinding-claude-ai-reads",
        metadata=ApiObjectMetadata(
            name="claude-ai-reads",
            namespace=_NAMESPACE,
            annotations={
                "description": "Auto-approves the reviewed GitHub/Home Assistant/Gmail/Calendar/Tana/Grocy SF reads and sandbox use for Connections acting as the claude-ai ServiceAccount."
            },
        ),
        spec=ActionPolicyBindingSpec(
            subject=ActionPolicyBindingSpecSubject(namespace=_NAMESPACE, name="claude-ai"),
            policy_sets=[
                _GITHUB_READS_SET,
                _GITHUB_IDENTITY_READS_SET,
                _SANDBOX_SET,
                _HOME_ASSISTANT_READS_SET,
                _GMAIL_READS_SET,
                _GOOGLE_CALENDAR_READS_SET,
                _TANA_READS_SET,
                _GROCY_SF_READS_SET,
            ],
        ),
    )
