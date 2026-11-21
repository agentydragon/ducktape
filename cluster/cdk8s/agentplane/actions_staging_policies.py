"""Staging-only Action Service policy objects: standing caller Identities, reviewed PolicySets,
and their bindings; also what their boxes may reach (egress) and read (the Coinbase key). See
cluster/k8s/agentplane-staging/README.md § Action policies -- Sandbox-subject bindings are written
by the integration app at runtime and are never checked in here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentplane_policybinding_crds.works.allegedly.agentplane import (
    PolicyBinding,
    PolicyBindingSpec,
    PolicyBindingSpecSubject,
)
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySet,
    PolicySetSpec,
    PolicySetSpecRules,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from agentplane.action_service.policies.resources import parse_action_rule
from agentplane.action_service.sandbox.contract.actions import Action
from agentplane.crd_group import GROUP
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import agent_access_profiles as access, external_creds
from cluster.cdk8s.agentplane import kubernetes_account_ref
from cluster.cdk8s.agentplane.actions import ORCHESTRATION_GROUP, SANDBOX_GROUP
from cluster.cdk8s.agentplane.egress import (
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    PACKAGES_POLICY,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import (
    ACTIVITYWATCH_READ_POLICY,
    AIQUOTA_READ_POLICY,
    FORGEJO_HAKU_POLICY,
    GOOGLE_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    QUEST_SCHEDULER_READ_POLICY,
    THRIVE_READ_POLICY,
    grocy_sf_read_egress_rules,
    home_assistant_read_egress_rules,
)
from cluster.cdk8s.agentplane.standing_caller import StandingCaller
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.monitoring import mimir
from cluster.cdk8s.providers.agentplane.identity import Identity
from cluster.cdk8s.providers.agentplane.policy_set import AutoApproveIf, Rule
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.public_coder.egress import OPENCLAW

_NAMESPACE = "agentplane-staging"
# Exported: launch presets (staging_config.py) bind these by name.
GITHUB_READS_SET = "github-reads"
SANDBOX_SELF_SET = "sandbox-self"
HOME_ASSISTANT_READS_SET = "home-assistant-reads"
GMAIL_READS_SET = "gmail-reads"
GOOGLE_CALENDAR_READS_SET = "google-calendar-reads"
TANA_READS_SET = "tana-reads"
GROCY_SF_READS_SET = "grocy-sf-reads"
KUBERNETES_INSPECTION_READS_SET = "kubernetes-inspection-reads"
# The `cluster-sops-read` Coinbase CDP key, which can only view (no trade, no transfer): the one
# Haku's sandbox reads too. cluster/cdk8s/external_creds.py approves this namespace's copy.
_COINBASE_SECRET = "coinbase-api-credentials"
_GITHUB_DOWNLOADS_POLICY = "github-downloads"
MIMIR_READS_POLICY = "mimir-reads"
# AnkiWeb sync for haku-anki, which runs as the haku-ui account (see the binding below).
ANKIWEB_SYNC_POLICY = "ankiweb-sync"
HAKU_CI_BUILD_FETCH_POLICY = "haku-ci-build-fetch"
# Package and code hosts for haku-jupyter, which runs as the haku-ui account (see the binding below).
HAKU_NOTEBOOK_FETCH_POLICY = "haku-notebook-fetch"

# Staging-only Action PolicySets that the launch presets (staging_config.py) and the Identity
# bindings below bind by name.
GITHUB_IDENTITY_READS_SET = "github-identity-reads"
SSH_READS_SET = "ssh-reads"
ORCHESTRATION_PRESETS_SET = "orchestration-presets"

CLAUDE_AI = StandingCaller(ServiceAccountRef(namespace=_NAMESPACE, name="claude-ai"), identity="claude-ai")
HAKU_AGENT = StandingCaller(ServiceAccountRef(namespace=_NAMESPACE, name="haku-agent"), identity="haku-agent")


def coinbase_egress_rules() -> list[PolicySetSpecRules]:
    """GET-only access to Coinbase; callers sign requests with the key held in Kubernetes."""
    return [Rule.egress(hosts=["api.coinbase.com"], methods=[PolicySetSpecRulesMethods.GET])]


# The Action PolicySets created below that the launch presets bind, named because a
# preset refers to them by name. They live here rather than in the preset's own module so the
# name and the object that answers to it are declared in one place: staging's presets import
# these, never the other way round.
COMMON_GITHUB_READ_SET = "common-github-read"
DUCKTAPE_PR_FAILED_JOBS_SET = "ducktape-pr-failed-jobs"
GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET = "gaffer-private-branch-and-pr-creation"

# The reviewed Action reads and sandbox use bound to the claude-ai and haku-agent Identities. The
# binding's existence is the grant: deleting it puts every one of these Actions back on the human path.
HAKU_SHARED_ACTION_SETS = (
    GITHUB_READS_SET,
    GITHUB_IDENTITY_READS_SET,
    SANDBOX_SELF_SET,
    HOME_ASSISTANT_READS_SET,
    GMAIL_READS_SET,
    GOOGLE_CALENDAR_READS_SET,
    TANA_READS_SET,
    GROCY_SF_READS_SET,
    SSH_READS_SET,
    KUBERNETES_INSPECTION_READS_SET,
)

HAKU_SHARED_CORE_POLICIES = (
    FORGEJO_HAKU_POLICY,
    PACKAGES_POLICY,
    GOOGLE_READONLY_POLICY,
    ACTIVITYWATCH_READ_POLICY,
    AIQUOTA_READ_POLICY,
    QUEST_SCHEDULER_READ_POLICY,
    THRIVE_READ_POLICY,
    HAKU_MAILBOX_POLICY,
)

HAKU_SHARED_GITHUB_POLICIES = (GITHUB_CLONE_POLICY, GITHUB_AGENTYDRAGON_AGENT_POLICY, GITHUB_ACTIONS_LOGS_POLICY)

_HAKU_CLAUDE_BASE_POLICIES = (
    BASIC_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    *HAKU_SHARED_CORE_POLICIES,
    access.COINBASE_POLICY,
    access.PLAID_DIAGNOSTICS_POLICY,
    _GITHUB_DOWNLOADS_POLICY,
    *HAKU_SHARED_GITHUB_POLICIES,
)


def caller_role_sets() -> list[str]:
    """The exact role sets granted to `claude-ai`'s and `haku-agent`'s Kubernetes accounts."""
    return [name for name in _HAKU_CLAUDE_BASE_POLICIES if name in access.POLICY_SETS]


def _identity_caller_policies(
    policies: Sequence[str], *, identity_catalog: bool, identity_egress_set_names: set[str]
) -> list[str]:
    """Identity-compatible policies, replacing mixed Kubernetes sets with their pure-egress sibling."""
    selected: list[str] = []
    for name in policies:
        if name not in access.POLICY_SETS:
            selected.append(name)
        elif name in identity_egress_set_names:
            selected.append(name if identity_catalog else f"{name}-identity")
    return selected


def _kubernetes_account_use(caller: StandingCaller) -> list[str]:
    """The use set of `caller`'s own Kubernetes account, named after its Identity."""
    return [kubernetes_account_ref.account(_NAMESPACE, caller.identity).credential_name]


def _policy_set(scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[PolicySetSpecRules]) -> None:
    # The Action Service parses a rule more strictly than the CRD schema (an unknown policy
    # kind or key, an invalid JSON Schema); a set it refuses grants nothing on the cluster, so
    # it fails here instead.
    policy_set_object = PolicySet(scope, id, metadata=metadata, spec=PolicySetSpec(rules=rules))
    for rule in policy_set_object.to_json()["spec"]["rules"]:
        parse_action_rule(rule)


def _exact_actions_policy_set(
    scope: Construct,
    name: str,
    *,
    description: str,
    actions: Mapping[str, Sequence[str]],
    additional_rules: Sequence[PolicySetSpecRules] = (),
) -> None:
    """Create a named staging PolicySet for an exact Action allowlist."""
    _policy_set(
        scope,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE, annotations={"description": description}),
        rules=[AutoApproveIf.exact_actions(actions=actions), *additional_rules],
    )


def _account_binding(
    scope: Construct, account: ServiceAccount, *, subject: PolicyBindingSpecSubject, policies: Sequence[str]
) -> None:
    """What `account`, and every sandbox running as it, may reach and have auto-approved."""
    PolicyBinding(
        scope,
        f"policybinding-{account.name}",
        metadata=ApiObjectMetadata(
            name=account.name,
            namespace=_NAMESPACE,
            annotations={
                "description": f"What the {account.name} ServiceAccount, and sandboxes running as it, may reach and have auto-approved."
            },
        ),
        spec=PolicyBindingSpec(subject=subject, policy_sets=policies),
    )


# GitHub MCP's normal endpoint exposes its default catalog, including writes. This is the
# explicit 2026-08-14 upstream read-only subset: new upstream tools intentionally stay
# manual until reviewed here. `ui_get` reads an MCP App UI resource, not GitHub
# repository state.
_GITHUB_READS_ACTIONS = [
    # keep-sorted start
    "find_duplicate",
    "get_commit",
    "get_file_blame",
    "get_file_contents",
    "get_label",
    "get_latest_release",
    "get_me",
    "get_release_by_tag",
    "get_tag",
    "get_team_members",
    "get_teams",
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
    "search_code",
    "search_commits",
    "search_issues",
    "search_pull_requests",
    "search_repositories",
    "search_users",
    "ui_get",
    # keep-sorted end
]

# The tool list every repository-scoped read set shares -- only the trusted owner/repo
# (or a live visibility check) differs.
_REPOSITORY_SCOPED_ACTIONS = [
    # keep-sorted start
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
    "search_code",
    "search_issues",
    "search_pull_requests",
    # keep-sorted end
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
    # keep-sorted start
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
    # keep-sorted end
]

# Gmail's generated read-only surface (x/google_mcp_server/gmail.py's _GMAIL_READ_TOOLS); writes
# (drafts_create/update/delete, threads_modify_labels, labels_create/patch/delete,
# filters_create/delete) stay on the human path.
_GMAIL_READS_ACTIONS = [
    # keep-sorted start
    "drafts_get",
    "drafts_list",
    "filters_get",
    "filters_list",
    "labels_get",
    "labels_list",
    "messages_get",
    "threads_get",
    "threads_list",
    # keep-sorted end
]

# Google Calendar's read-only surface (x/google_mcp_server/google_calendar.py); create_event
# stays on the human path.
_GOOGLE_CALENDAR_READS_ACTIONS = ["get_event", "list_event_instances", "list_events"]

# Tana's read-only surface, plus `get_or_create_calendar_node`, whose only write is creating a
# date's calendar node when it is missing. Reviewed exclusion: `open_node` navigates the
# operator's Tana desktop app. New upstream tools stay manual until reviewed here.
_TANA_READS_ACTIONS = [
    # keep-sorted start
    "get_children",
    "get_or_create_calendar_node",
    "get_tag_schema",
    "list_tags",
    "list_workspaces",
    "read_node",
    "search_nodes",
    # keep-sorted end
]

# Grocy SF's read-only surface. Reviewed exclusion: `open_product_stock` marks a product opened.
# New upstream tools stay manual until reviewed here.
_GROCY_SF_READS_ACTIONS = [
    # keep-sorted start
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
    # keep-sorted end
]

# kubectl-passthrough-mcp's node and Pod resource-usage reads: metrics only, no object contents,
# logs or Secrets. They execute as the operator's cluster-admin identity, so they cover every node
# and namespace. Every other kubernetes_admin Action stays manual until reviewed here.
_KUBERNETES_INSPECTION_READS_ACTIONS = ["nodes_stats_summary", "nodes_top", "pods_top"]


def add_staging_action_policies(
    scope: Construct,
    *,
    identity_catalog: bool,
    identity_egress_set_names: set[str],
    haku_ui: Identity,
    haku_ci: Identity,
) -> None:
    """Render Haku's standing workload Identities and caller grants independently of the optional catalog."""
    # The principal a Connection enrolled from the Claude.ai MCP connector acts as, picked by
    # the operator at OAuth consent, and the Identity its boxes run as. The reviewed Identity
    # binding grants auto-approval; this ServiceAccount has no Action-caller label.
    claude_ai = ServiceAccount(
        scope,
        "serviceaccount-claude-ai",
        metadata=ApiObjectMetadata(
            name=CLAUDE_AI.service_account.name,
            namespace=CLAUDE_AI.service_account.namespace,
            annotations={
                "description": "The principal for Connections enrolled from the Claude.ai MCP connector, selected at OAuth consent."
            },
        ),
        automount_token=False,
    )

    # A dedicated identity for the "haku" sandbox preset (staging_config.py), which clones
    # haku-state and works from there the way Haku itself does. Separate from claude-ai
    # rather than reusing it: claude-ai is the Claude.ai MCP connector's principal, and
    # Haku's own credentials (forgejo-haku, haku-mailbox) are bound to claude-ai today only
    # because claude-ai happens to be "the connection Haku runs through" (see the
    # egress PolicyBinding comment below) -- a historical accident, not a reason to keep growing
    # that account's authority. haku-agent is a static caller identity, not the per-Sandbox
    # ServiceAccount on managed runner Pods; Coinbase key delivery to those runners is tracked
    # in #8596.
    haku_agent = ServiceAccount(
        scope,
        "serviceaccount-haku-agent",
        metadata=ApiObjectMetadata(
            name=HAKU_AGENT.service_account.name,
            namespace=HAKU_AGENT.service_account.namespace,
            annotations={
                "description": "The principal for the 'haku' sandbox preset: agents that clone haku-state and work from it, the way Haku itself does."
            },
        ),
        automount_token=False,
    )

    _exact_actions_policy_set(
        scope,
        GITHUB_READS_SET,
        description="The reviewed read-only subset of GitHub MCP's default catalog; every other GitHub Action stays on the human path.",
        actions={"github": _GITHUB_READS_ACTIONS},
    )
    # `get_me` returns only the authenticated caller's own GitHub identity and has no
    # repository or mutation surface. Kept separate so a caller can be granted the
    # identity read without widening repository-scoped GitHub sets.
    _exact_actions_policy_set(
        scope,
        GITHUB_IDENTITY_READS_SET,
        description="The caller's own GitHub identity read, with no repository or mutation surface.",
        actions={"github": ["get_me"]},
    )
    # The GitHub reads every coder-like agent gets: any repository a live, unauthenticated lookup
    # confirms public (`github_public_repository` positively confirms visibility rather than
    # inferring it from the absence of a restriction), plus three named repositories: ducktape,
    # the coder Agent's own fork of it (which it can already write, so reading it adds no exposure),
    # and the private gaffer-private.
    _policy_set(
        scope,
        "common-github-read",
        metadata=ApiObjectMetadata(
            name=COMMON_GITHUB_READ_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "Reviewed GitHub reads of any repository a live unauthenticated lookup confirms public, and of agentydragon/ducktape, its fork agentydragon-agent/ducktape and the private agentydragon/gaffer-private."
            },
        ),
        rules=[
            AutoApproveIf.github_public_repository(actions={"github": _REPOSITORY_SCOPED_ACTIONS}),
            *(
                AutoApproveIf.github_repository(
                    owner=owner, repository=repository, actions={"github": _REPOSITORY_SCOPED_ACTIONS}
                )
                for owner, repository in (
                    ("agentydragon", "ducktape"),
                    ("agentydragon-agent", "ducktape"),
                    ("agentydragon", "gaffer-private"),
                )
            ),
        ],
    )

    # GitHub MCP combines workflow dispatch, whole-run reruns, failed-job reruns, cancellation,
    # and log deletion in one Action. Restrict this grant to the failed-job method and the
    # ducktape repository; the other operations remain on the human path. This also covers PR CI
    # runs, though the MCP arguments do not expose a run's originating event for PR-only filtering.
    _policy_set(
        scope,
        "ducktape-pr-failed-jobs",
        metadata=ApiObjectMetadata(
            name=DUCKTAPE_PR_FAILED_JOBS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Auto-approves only GitHub MCP rerun_failed_jobs calls for agentydragon/ducktape; "
                    "dispatch, whole-run rerun, cancellation and log deletion remain on the human path."
                )
            },
        ),
        rules=[
            AutoApproveIf.argument_schema(
                actions={"github": ["actions_run_trigger"]},
                schema={
                    "type": "object",
                    "properties": {
                        "method": {"const": "rerun_failed_jobs"},
                        "owner": {"const": "agentydragon"},
                        "repo": {"const": "ducktape"},
                        "run_id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["method", "owner", "repo", "run_id"],
                },
            )
        ],
    )

    # Two additive GitHub writes in the private Gaffer repository, and nothing else; pushing
    # commits, editing files, merging and reviewer requests remain on the human-approval path.
    #  - create_branch: GitHub's CreateRef rejects an existing branch name rather than moving that ref.
    #  - create_pull_request: opening a PR creates a review object but does not merge it or change
    #    either branch. It may notify reviewers and trigger repository workflows, so approval is
    #    scoped to this repository and this one Action.
    _policy_set(
        scope,
        "gaffer-private-branch-and-pr-creation",
        metadata=ApiObjectMetadata(
            name=GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Auto-approves only GitHub MCP create_branch and create_pull_request calls targeting "
                    "agentydragon/gaffer-private; existing branches cannot be overwritten, the PR is not "
                    "merged, and other writes remain human-approved."
                )
            },
        ),
        rules=[
            AutoApproveIf.argument_schema(
                actions={"github": ["create_branch"]},
                schema={
                    "type": "object",
                    "properties": {
                        "owner": {"const": "agentydragon"},
                        "repo": {"const": "gaffer-private"},
                        "branch": {"type": "string", "minLength": 1},
                        "from_branch": {"type": "string", "minLength": 1},
                    },
                    "required": ["owner", "repo", "branch"],
                    "additionalProperties": False,
                },
            ),
            AutoApproveIf.argument_schema(
                actions={"github": ["create_pull_request"]},
                schema={
                    "type": "object",
                    "properties": {
                        "owner": {"const": "agentydragon"},
                        "repo": {"const": "gaffer-private"},
                        "title": {"type": "string", "minLength": 1},
                        "head": {"type": "string", "minLength": 1},
                        "base": {"type": "string", "minLength": 1},
                        "body": {"type": "string"},
                        "draft": {"type": "boolean"},
                        "maintainer_can_modify": {"const": False},
                    },
                    "required": ["owner", "repo", "title", "head", "base"],
                    "additionalProperties": False,
                },
            ),
        ],
    )

    # Coinbase authenticates each request with a fresh JWT signed over its method, host and path,
    # which the egress proxy's placeholder substitution cannot produce. The `coinbase` PolicySet
    # grants selected sandboxes read access to this Secret and GET egress to api.coinbase.com, so
    # they sign requests locally. What makes handing out the key acceptable is that it can only view.
    ExternalSecret(
        scope,
        "coinbase-external-secret",
        metadata=ApiObjectMetadata(
            name=_COINBASE_SECRET,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "ESO copy of the view-only Coinbase CDP key from external-creds, read by explicitly "
                    "authorized Haku, finance-agent, claude-ai, and haku-agent sandboxes."
                )
            },
        ),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(_COINBASE_SECRET, key) for key in ("api_key", "api_secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    coinbase_reader = Role(
        scope,
        "role-claude-ai-coinbase",
        metadata=ApiObjectMetadata(name="claude-ai-coinbase-reader", namespace=_NAMESPACE),
        rules=[
            RolePolicyRule(
                resources=[Secret.from_secret_name(scope, "coinbase-secret", _COINBASE_SECRET)], verbs=["get"]
            )
        ],
    )
    RoleBinding(
        scope,
        "rolebinding-claude-ai-coinbase",
        metadata=ApiObjectMetadata(name="claude-ai-coinbase-reader", namespace=_NAMESPACE),
        role=coinbase_reader,
    ).add_subjects(
        claude_ai,
        *(
            subject.imported(scope, f"coinbase-subject-{index}")
            for index, subject in enumerate(access.profile_subjects("coinbase-credentials"))
        ),
    )
    sandbox_reader = Role(
        scope,
        "role-claude-ai-sandbox-reader",
        metadata=ApiObjectMetadata(name="claude-ai-sandbox-reader", namespace=_NAMESPACE),
        rules=[
            RolePolicyRule(
                resources=[
                    custom_resource(GROUP, "sandboxes"),
                    custom_resource(GROUP, "sandboxtemplates"),
                    custom_resource(GROUP, "policysets"),
                    custom_resource(GROUP, "policybindings"),
                ],
                verbs=["get", "list"],
            ),
            # A `kind: vm` Sandbox's guest: state, and `virtctl console` for a runner that never connected.
            RolePolicyRule(
                resources=[
                    custom_resource("kubevirt.io", "virtualmachines"),
                    custom_resource("kubevirt.io", "virtualmachineinstances"),
                ],
                verbs=["get", "list", "watch"],
            ),
            RolePolicyRule(
                resources=[custom_resource("subresources.kubevirt.io", "virtualmachineinstances/console")],
                verbs=["get"],
            ),
        ],
    )
    RoleBinding(
        scope,
        "rolebinding-claude-ai-sandbox-reader",
        metadata=ApiObjectMetadata(name="claude-ai-sandbox-reader", namespace=_NAMESPACE),
        role=sandbox_reader,
    ).add_subjects(
        *(
            subject.imported(scope, f"sandbox-reader-subject-{index}")
            for index, subject in enumerate(access.profile_subjects("agentplane-sandbox-read"))
        )
    )
    # GitHub downloads with nothing substituted: a release asset or a tag archive, which is what a
    # Bazel `http_archive` fetches, without the write-capable PAT `github-agentydragon-agent` carries.
    PolicySet(
        scope,
        "egresspolicy-github-downloads",
        metadata=ApiObjectMetadata(name=_GITHUB_DOWNLOADS_POLICY, namespace=_NAMESPACE),
        spec=PolicySetSpec(
            rules=[
                Rule.egress(
                    hosts=[
                        "github.com",
                        # Where `github.com/.../archive/...` redirects.
                        "codeload.github.com",
                        # Where `github.com/.../releases/download/...` redirects.
                        "objects.githubusercontent.com",
                        "release-assets.githubusercontent.com",
                        # Raw file content off a ref, e.g. `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`.
                        "raw.githubusercontent.com",
                    ],
                    methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.HEAD],
                )
            ]
        ),
    )

    # Mimir's Prometheus-compatible read API over the cluster Service, with no credential: the
    # gateway admits any in-cluster caller, and this rule is the whole fence. GET only, so a
    # sandbox can query metrics but not push samples or reach the ruler and admin routes.
    PolicySet(
        scope,
        f"egresspolicy-{MIMIR_READS_POLICY}",
        metadata=ApiObjectMetadata(name=MIMIR_READS_POLICY, namespace=_NAMESPACE),
        spec=PolicySetSpec(
            rules=[
                Rule.egress(
                    hosts=[mimir.GATEWAY.fqdn],
                    methods=[PolicySetSpecRulesMethods.GET],
                    paths=["/prometheus/api/v1/**"],
                    cluster_internal=True,
                )
            ]
        ),
    )

    # What a box of this Identity may reach. Egress and Action grants bind to the Identity;
    # Kubernetes API rights stay behind its named account use set and exact ServiceAccountGrant.
    #
    # `forgejo-haku` is the widest of these by some distance: it substitutes the `haku` account's
    # own Forgejo password, so a sandbox of this caller's acts as haku across every repository that
    # account owns. It is here because the operator asked for it; it is not a default any caller
    # should inherit. `packages` is the opposite end: public mirrors, no credential, GET and HEAD.
    # `github-clone` is a strict subset of `github-agentydragon-agent`; kept bound so clone still
    # works if that credential is ever unbound again. `github-agentydragon-agent` (egress.py)
    # substitutes a write-capable PAT with no path limit, so a sandbox of this caller's can act as
    # that bot across the whole GitHub REST API and git protocol -- not just clone/push, and not
    # just create: it can edit and close what it opened -- by the operator's explicit choice, the
    # same shape of tradeoff `forgejo-haku` above already accepts for Forgejo.
    #
    # TODO: `forgejo-haku` and `haku-mailbox` stay bound here for now even though haku-agent
    # (below) exists specifically to run in-cluster Haku-like sandboxes: claude-ai is still the
    # identity the same *logical* Haku agent runs as from Claude.ai/Claude Code's own
    # out-of-cluster infrastructure, so it still needs Haku's credentials (haku/TODO.md). Revisit
    # whether claude-ai should keep carrying them once haku-agent has been in use for a while.
    _account_binding(
        scope,
        claude_ai,
        subject=CLAUDE_AI.identity_subject(),
        policies=[
            *_identity_caller_policies(
                _HAKU_CLAUDE_BASE_POLICIES,
                identity_catalog=identity_catalog,
                identity_egress_set_names=identity_egress_set_names,
            ),
            BUILDBUDDY_POLICY,
            MIMIR_READS_POLICY,
            # claude-ai is the account that launches orchestration_v1 workers.
            ORCHESTRATION_PRESETS_SET,
            *HAKU_SHARED_ACTION_SETS,
            *_kubernetes_account_use(CLAUDE_AI),
        ],
    )

    # haku-ui, Haku's operator UI, shares the Home Assistant and Grocy SF read sets with haku-anki.
    # Its legacy direct Action route is removed; its sidecar presents its standing SVID to the proxy.
    # haku-anki runs as this account too, for AnkiWeb sync: the one external host it needs (`sync.ankiweb.net`
    # and the `syncNN` shard hosts it redirects accounts to; `*.` never matches the apex, and these are
    # subdomains), through its egress sidecar. No credential is
    # presented: AnkiWeb login sends the account's own username and password in a compressed body that a
    # placeholder substitution cannot reach, so the real credentials stay in the Pod. haku-sandbox is one trust
    # domain, so the two workloads share a grant set: the haku-ui Pod can reach AnkiWeb and the haku-anki Pod
    # can read Home Assistant and Grocy and submit Action requests. This is the one grant claude-ai lacks.
    # Split the account if either
    # workload ever holds something the other must not.
    PolicySet(
        scope,
        "egresspolicy-ankiweb-sync",
        metadata=ApiObjectMetadata(name=ANKIWEB_SYNC_POLICY, namespace=_NAMESPACE),
        spec=PolicySetSpec(rules=[Rule.egress(hosts=["*.ankiweb.net"])]),
    )
    # haku-jupyter, Haku's notebook server, runs as this account too. It showed no public traffic through
    # haku-egress-proxy (2026-11-08/09), so these hosts are a starting point the operator widens: the package
    # indexes and code hosts a notebook plausibly installs or reads from, GET/HEAD only, nothing presented.
    # The haku-ui and haku-anki Pods gain them too; all are within claude-ai's `github-downloads` and `packages`.
    PolicySet(
        scope,
        f"egresspolicy-{HAKU_NOTEBOOK_FETCH_POLICY}",
        metadata=ApiObjectMetadata(name=HAKU_NOTEBOOK_FETCH_POLICY, namespace=_NAMESPACE),
        spec=PolicySetSpec(
            rules=[
                Rule.egress(
                    hosts=[
                        # keep-sorted start
                        "codeload.github.com",
                        "files.pythonhosted.org",
                        "github.com",
                        "objects.githubusercontent.com",
                        "pypi.org",
                        "raw.githubusercontent.com",
                        "registry.npmjs.org",
                        # keep-sorted end
                    ],
                    methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.HEAD],
                )
            ]
        ),
    )
    PolicyBinding(
        scope,
        "policybinding-haku-ui",
        metadata=ApiObjectMetadata(
            name=haku_ui.name,
            namespace=_NAMESPACE,
            annotations={
                "description": "What Haku's standing workloads (haku-sandbox), acting as this Identity, may reach and have auto-approved."
            },
        ),
        spec=PolicyBindingSpec(
            subject=haku_ui.binding_subject(),
            policy_sets=[HOME_ASSISTANT_READS_SET, GROCY_SF_READS_SET, ANKIWEB_SYNC_POLICY, HAKU_NOTEBOOK_FETCH_POLICY],
        ),
    )

    # haku-ci's builds, through its egress relay (haku_ci/egress_gateway.py): exactly the hosts haku-ci
    # reached through its former mitmproxy (haku-egress-proxy) over 2026-11-08/09 (about 60 CI jobs), each with the methods it
    # used. Add a host when its `no-rule` decision for the haku-ci-egress Identity appears. Docker Hub
    # stays out: dind pulls it through oci-cache, in-cluster.
    PolicySet(
        scope,
        "egresspolicy-haku-ci-build-fetch",
        metadata=ApiObjectMetadata(name=HAKU_CI_BUILD_FETCH_POLICY, namespace=_NAMESPACE),
        spec=PolicySetSpec(
            rules=[
                Rule.egress(
                    hosts=[
                        # keep-sorted start
                        # Bazel's registry and its own releases.
                        "bcr.bazel.build",
                        # Forgejo Actions job images (dind pulls).
                        "code.forgejo.org",
                        "codeload.github.com",
                        "files.pythonhosted.org",
                        "github.com",
                        # rules_oci's gawk source.
                        "mirrors.kernel.org",
                        "nodejs.org",
                        "pypi.org",
                        "registry.npmjs.org",
                        "release-assets.githubusercontent.com",
                        "releases.bazel.build",
                        # keep-sorted end
                    ],
                    methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.HEAD],
                ),
                # `uses:` actions, which forgejo-runner clones over smart HTTP (`git-upload-pack` is a POST).
                Rule.egress(
                    hosts=["data.forgejo.org"], methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.POST]
                ),
            ]
        ),
    )
    PolicyBinding(
        scope,
        "policybinding-haku-ci-egress",
        metadata=ApiObjectMetadata(
            name=haku_ci.name,
            namespace=_NAMESPACE,
            annotations={
                "description": "What haku-ci's builds (haku-ci namespace), acting as this Identity, may reach."
            },
        ),
        spec=PolicyBindingSpec(subject=haku_ci.binding_subject(), policy_sets=[HAKU_CI_BUILD_FETCH_POLICY]),
    )

    # Haku-like sandboxes get Coinbase only through this explicit combined PolicySet selection.
    _account_binding(
        scope,
        haku_agent,
        subject=HAKU_AGENT.identity_subject(),
        policies=[
            *_identity_caller_policies(
                _HAKU_CLAUDE_BASE_POLICIES,
                identity_catalog=identity_catalog,
                identity_egress_set_names=identity_egress_set_names,
            ),
            *HAKU_SHARED_ACTION_SETS,
            *_kubernetes_account_use(HAKU_AGENT),
        ],
    )

    # Every sandbox Action, auto-approved. Approving each one individually would not be a
    # boundary: a sandbox runs as the account that asked for it, so the whole group can do
    # exactly what that account can already do, and `exec` inside it is not narrower than
    # `provision` of it. What decides whether this is safe for an account is that account's own
    # egress and Kubernetes authority, which is why this set is bound per caller and not by default.
    _exact_actions_policy_set(
        scope,
        SANDBOX_SELF_SET,
        description=(
            "Auto-approves every sandbox Action: a caller's own boxes, which act only as that caller, "
            "and reads of the templates they are made from."
        ),
        actions={SANDBOX_GROUP: sorted(action.value for action in Action)},
    )

    # Home Assistant's read-only surface (see _HOME_ASSISTANT_READS_ACTIONS above for the
    # reviewed tool list and exclusions).
    _exact_actions_policy_set(
        scope,
        HOME_ASSISTANT_READS_SET,
        description="The reviewed read-only subset of Home Assistant MCP's default catalog, and the read-only REST routes the egress proxy presents Home Assistant's reader token on; every other Home Assistant Action stays on the human path.",
        actions={"home_assistant": _HOME_ASSISTANT_READS_ACTIONS},
        additional_rules=home_assistant_read_egress_rules(),
    )

    # Gmail's and Google Calendar's read-only surfaces (see the _GMAIL_READS_ACTIONS /
    # _GOOGLE_CALENDAR_READS_ACTIONS lists above for the reviewed tool lists and exclusions).
    _exact_actions_policy_set(
        scope,
        GMAIL_READS_SET,
        description="The reviewed read-only subset of the Gmail MCP backend's catalog; every write stays on the human path.",
        actions={"gmail": _GMAIL_READS_ACTIONS},
    )
    _exact_actions_policy_set(
        scope,
        GOOGLE_CALENDAR_READS_SET,
        description="The reviewed read-only subset of the Google Calendar MCP backend's catalog; create_event stays on the human path.",
        actions={"google_calendar": _GOOGLE_CALENDAR_READS_ACTIONS},
    )

    _exact_actions_policy_set(
        scope,
        TANA_READS_SET,
        description="The reviewed read-only subset of the Tana MCP backend's catalog, plus get_or_create_calendar_node; every other write and open_node stay on the human path.",
        actions={"tana": _TANA_READS_ACTIONS},
    )
    _exact_actions_policy_set(
        scope,
        GROCY_SF_READS_SET,
        description="The reviewed read-only subset of the Grocy SF MCP backend's catalog, and the GET-only REST routes the egress proxy presents Grocy's service-account password on; every write, open_product_stock included, stays on the human path.",
        actions={"grocy_sf": _GROCY_SF_READS_ACTIONS},
        additional_rules=grocy_sf_read_egress_rules(),
    )

    # SSH MCP's `list_targets`: it names which machine/user targets are configured and whether
    # each one's local key is present -- no hostname is dialed, no command runs, and no secret
    # material is returned. Every other `ssh` Action (running a command against a target) stays
    # on the human path.
    _exact_actions_policy_set(
        scope,
        SSH_READS_SET,
        description="Auto-approves the SSH MCP backend's list_targets, which only names configured targets and key availability; every other SSH Action stays on the human path.",
        actions={"ssh": ["list_targets"]},
    )

    _exact_actions_policy_set(
        scope,
        ORCHESTRATION_PRESETS_SET,
        description=(
            "Auto-approves orchestration_v1's list_launch_presets, which only lists the reviewed launch "
            "presets; a launch always waits for the operator."
        ),
        actions={ORCHESTRATION_GROUP: ["list_launch_presets"]},
    )

    _exact_actions_policy_set(
        scope,
        KUBERNETES_INSPECTION_READS_SET,
        description=(
            "Auto-approves kubernetes_admin's node stats and node/Pod resource-usage reads; every "
            "other kubernetes_admin Action stays on the human path."
        ),
        actions={"kubernetes_admin": _KUBERNETES_INSPECTION_READS_ACTIONS},
    )

    # The OpenClaw Pod is hosted in its own namespace; keep its Action authorization here with
    # the shared policy sets, addressed by its standing Identity.
    PolicyBinding(
        scope,
        "policybinding-public-coder-openclaw-actions",
        metadata=ApiObjectMetadata(
            name="public-coder-openclaw-actions",
            namespace=_NAMESPACE,
            annotations={"description": "Auto-approves the reviewed common GitHub reads for the OpenClaw Identity."},
        ),
        spec=PolicyBindingSpec(subject=OPENCLAW.identity_subject(), policy_sets=[COMMON_GITHUB_READ_SET]),
    )
