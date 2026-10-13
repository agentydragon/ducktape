"""Staging-only Action Service policy objects: caller ServiceAccounts, reviewed
PolicySets, and their bindings; also what its sandboxes may reach (its egress PolicyBinding)
and read (the Coinbase key). See cluster/k8s/agentplane-staging/README.md § Action policies --
Sandbox-subject bindings are written by the integration app at runtime and are never checked in here.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from agentplane_policybinding_crds.works.allegedly.agentplane import PolicyBindingSpecSubject
from agentplane_policyset_crds.works.allegedly.agentplane import PolicySetSpecRules, PolicySetSpecRulesMethods
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from agentplane.action_service.policies.resources import CALLER_LABEL, parse_action_rule
from agentplane.action_service.sandbox.actions import SANDBOX_GROUP, SandboxAction
from agentplane.crd_group import GROUP
from cluster.cdk8s import agent_access_profiles as access, external_creds
from cluster.cdk8s.agentplane import app as app_component, dex, egress, testing
from cluster.cdk8s.agentplane.egress import (
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
    PACKAGES_POLICY,
)
from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.agentplane.egress_staging_credentials import (
    ACTIVITYWATCH_READ_POLICY,
    AIQUOTA_READ_POLICY,
    FORGEJO_HAKU_POLICY,
    GOOGLE_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    PLAID_PGWEB_POLICY,
    grocy_sf_read_egress_rules,
    home_assistant_read_egress_rules,
)
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.providers.agentplane import policy_set
from cluster.cdk8s.providers.agentplane.policy_binding import PolicyBinding
from cluster.cdk8s.providers.agentplane.policy_set import AutoApproveIf, Rule
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.public_coder.egress import NAMESPACE, SERVICE_ACCOUNT

_NAMESPACE = "agentplane-staging"
# Exported: launch presets (staging_config.py) bind these by name.
GITHUB_READS_SET = "github-reads"
SANDBOX_SELF_SET = "sandbox-self"
HOME_ASSISTANT_READS_SET = "home-assistant-reads"
GMAIL_READS_SET = "gmail-reads"
GOOGLE_CALENDAR_READS_SET = "google-calendar-reads"
TANA_READS_SET = "tana-reads"
GROCY_SF_READS_SET = "grocy-sf-reads"
# The `cluster-sops-read` Coinbase CDP key, which can only view (no trade, no transfer): the one
# Haku's sandbox reads too. cluster/cdk8s/external_creds.py approves this namespace's copy.
_COINBASE_SECRET = "coinbase-api-credentials"
_GITHUB_DOWNLOADS_POLICY = "github-downloads"

# The staging-only policies created below. `agentplane-testing` lets a staging sandbox reach the
# testing instance and `coinbase` publishes the Coinbase key, so their names belong to this
# module; so do the two Action PolicySet reads every preset binds by default.
AGENTPLANE_TESTING_POLICY = "agentplane-testing"
COINBASE_POLICY = "coinbase"
GITHUB_IDENTITY_READS_SET = "github-identity-reads"
SSH_READS_SET = "ssh-reads"

# The Action PolicySets created below that the launch presets bind, named because a
# preset refers to them by name. They live here rather than in the preset's own module so the
# name and the object that answers to it are declared in one place: staging's presets import
# these, never the other way round.
COMMON_GITHUB_READ_SET = "common-github-read"
DUCKTAPE_PR_FAILED_JOBS_SET = "ducktape-pr-failed-jobs"
GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET = "gaffer-private-branch-and-pr-creation"

# The reviewed Action reads and sandbox use bound to the claude-ai and haku-agent accounts. The
# binding's existence is the grant: deleting it, or the label on the ServiceAccount, puts every
# one of these Actions back on the human path.
_ACCOUNT_READ_SETS = (
    GITHUB_READS_SET,
    GITHUB_IDENTITY_READS_SET,
    SANDBOX_SELF_SET,
    HOME_ASSISTANT_READS_SET,
    GMAIL_READS_SET,
    GOOGLE_CALENDAR_READS_SET,
    TANA_READS_SET,
    GROCY_SF_READS_SET,
    SSH_READS_SET,
)


def _policy_set(scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[PolicySetSpecRules]) -> None:
    # The Action Service parses a rule more strictly than the CRD schema (an unknown policy
    # kind or key, an invalid JSON Schema); a set it refuses grants nothing on the cluster, so
    # it fails here instead.
    policy_set_object = policy_set.PolicySet(scope, id, metadata=metadata, rules=rules)
    for rule in policy_set_object.to_json()["spec"]["rules"]:
        parse_action_rule(rule)


def _binding(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    subject: PolicyBindingSpecSubject,
    policy_sets: Sequence[str],
) -> None:
    PolicyBinding(scope, id, metadata=metadata, subject=subject, policy_sets=policy_sets)


def _account_binding(scope: Construct, account: ServiceAccount, *, policies: Sequence[str]) -> None:
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
        subject=PolicyBindingSpecSubject(namespace=_NAMESPACE, name=account.name),
        policy_sets=policies,
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

# Grocy SF's read-only surface, as haku-console's `grocy_reads` policy auto-approved it.
# Reviewed exclusion: `open_product_stock` marks a product opened. New upstream tools stay manual
# until reviewed here.
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


def add_staging_action_policies(scope: Construct) -> None:
    # The principal a Connection enrolled from the Claude.ai MCP connector acts as, picked by
    # the operator at OAuth consent, and the identity its sandboxes run as. The label is what
    # makes it an Action caller, and removing the label or the object is how it is disabled.
    # What it may do without an operator is a PolicyBinding naming it; its Kubernetes
    # access is listed in cluster/docs/agent_rbac.md § 6.
    claude_ai = ServiceAccount(
        scope,
        "serviceaccount-claude-ai",
        metadata=ApiObjectMetadata(
            name="claude-ai",
            namespace=_NAMESPACE,
            labels={CALLER_LABEL: "true"},
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
            name="haku-agent",
            namespace=_NAMESPACE,
            labels={CALLER_LABEL: "true"},
            annotations={
                "description": "The principal for the 'haku' sandbox preset: agents that clone haku-state and work from it, the way Haku itself does."
            },
        ),
        automount_token=False,
    )

    _policy_set(
        scope,
        "github-reads",
        metadata=ApiObjectMetadata(
            name=GITHUB_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of GitHub MCP's default catalog; every other GitHub Action stays on the human path."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"github": _GITHUB_READS_ACTIONS})],
    )
    # `get_me` returns only the authenticated caller's own GitHub identity and has no
    # repository or mutation surface. Kept separate so a caller can be granted the
    # identity read without widening repository-scoped GitHub sets.
    _policy_set(
        scope,
        "github-identity-reads",
        metadata=ApiObjectMetadata(
            name=GITHUB_IDENTITY_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The caller's own GitHub identity read, with no repository or mutation surface."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"github": ["get_me"]})],
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
    # which the egress proxy's placeholder substitution cannot produce. So a sandbox of claude-ai's
    # holds the key and signs for itself: it may read this one Secret through the API server, and
    # the `coinbase` policy passes its GETs to api.coinbase.com unchanged. What makes handing the
    # sandbox the key acceptable is that the key can only view.
    ExternalSecret(
        scope,
        "coinbase-external-secret",
        metadata=ApiObjectMetadata(
            name=_COINBASE_SECRET,
            namespace=_NAMESPACE,
            annotations={
                "description": "ESO copy of the view-only Coinbase CDP key from external-creds, read by claude-ai's sandboxes."
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
        rules=[RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list"])],
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
    add_egress_policy(
        scope,
        "egresspolicy-coinbase",
        metadata=ApiObjectMetadata(name=COINBASE_POLICY, namespace=_NAMESPACE),
        rules=[Rule.egress(hosts=["api.coinbase.com"], methods=[PolicySetSpecRulesMethods.GET])],
    )
    # The testing deployment's app, for the acceptance suite's harness scenarios run from a box
    # (agentplane/acceptance/README.md): by its Service rather than its public name, which would
    # hairpin out through the Gateway and back. Plain HTTP, so the proxy reads the request without
    # bumping TLS. Nothing is substituted: the suite presents the app token it mints in
    # agentplane-testing (testing.AcceptanceToken), so any method may pass.
    testing_app = app_component.service(testing.ENV.namespace)
    # Browser/OIDC acceptance tests follow the app's public callback and Dex issuer.
    # Neither staging's app nor its real Authentik issuer belongs in this policy.
    testing_login_hosts = [testing.ENV.app.hostname, urlsplit(testing.ENV.app.oidc_issuer).netloc]
    add_egress_policy(
        scope,
        "egresspolicy-agentplane-testing",
        metadata=ApiObjectMetadata(name=AGENTPLANE_TESTING_POLICY, namespace=_NAMESPACE),
        # The proxy matches a request's host on the exact string, and clients spell the full name.
        rules=[
            Rule.egress(hosts=[testing_app.fqdn], cluster_internal=True),
            Rule.egress(hosts=testing_login_hosts, cluster_internal=True),
        ],
    )
    proxy = egress.proxy(_NAMESPACE)
    # No `serverNames` on node:443 here, deliberately. This selector is the staging egress
    # proxy's own Pods, and `egress.py` already gives them open `[world, remote-node, host]:443/80`.
    # An SNI rule added on top of that is an L7 rule for the whole endpoint: it decides every
    # node-IP HTTPS the proxy makes, resetting every `*.allegedly.works` name nobody typed into the
    # list. That pair is what made Grocy, Forgejo, Haku's mailbox, ActivityWatch and aiquota
    # unreachable from sandboxes for two weeks. `fleet_rules.https_egress_sni_conflicts` refuses it
    # at synth now; the mechanism is cluster/docs/cilium_network_policy.md section "An SNI rule
    # decides port 443 for every policy that selects the same Pod". The proxy fences those names
    # where it is meant to: in its own egress PolicySets.
    NetworkPolicy(
        scope,
        "networkpolicy-egress-to-testing-app",
        metadata=ApiObjectMetadata(name=f"{proxy.name}-to-testing-app", namespace=_NAMESPACE),
        endpoint_selector=proxy.pods.selector,
        egress=[
            testing_app.egress(),
            # Gateway Service traffic is checked against the selected backend, not
            # node:443. Keep Dex's backend permission scoped to its login SNI. This rule is
            # Dex's own port, so its SNI scoping touches nothing else the proxy reaches.
            EgressRule.to_endpoints(
                dex.service().pods.cilium,
                dex.service().pod_port,
                server_names=[urlsplit(testing.ENV.app.oidc_issuer).netloc],
            ),
        ],
    )
    # GitHub downloads with nothing substituted: a release asset or a tag archive, which is what a
    # Bazel `http_archive` fetches, without the write-capable PAT `github-agentydragon-agent` carries.
    add_egress_policy(
        scope,
        "egresspolicy-github-downloads",
        metadata=ApiObjectMetadata(name=_GITHUB_DOWNLOADS_POLICY, namespace=_NAMESPACE),
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
        ],
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
    # that module's `grocy-sf-readonly` egress PolicySet for why that's Grocy's API rather than the
    # grocy-mcp-sf MCP server). `activitywatch-read` presents the ActivityWatch read route's bearer,
    # which that route itself holds to reads, and `aiquota-read` aiquota's bearer on its read-only
    # API. `haku-mailbox` presents the JWT of Haku's own mailbox: JMAP reads and changes that one
    # mailbox and cannot send. It and `forgejo-haku` are Haku's credentials, bound here because
    # claude-ai is the connection Haku runs through (haku/TODO.md). `coinbase` presents nothing: the
    # sandbox signs with the key above. `plaid-pgweb` presents pgweb's HTTP Basic password on its
    # query API, so a sandbox of this caller's can run SQL over the Plaid mirror as `plaid_ro`,
    # which can only SELECT (egress_staging_credentials.py).
    # `agentplane-testing` presents nothing either: the acceptance suite brings its own app token.
    # `github-downloads` presents nothing either: public GitHub downloads, GET and HEAD only.
    # `github-clone` presents nothing either: the anonymous smart-HTTP git protocol
    # (GET+POST to github.com only, scoped to the `info/refs`/`git-upload-pack` paths). Now a
    # strict subset of `github-agentydragon-agent` below; kept bound so clone still works if
    # that credential is ever unbound again.
    # `github-agentydragon-agent` substitutes the same write-capable `agentydragon-agent` PAT
    # `public-coder` already carries (egress.py): GET and POST to api.github.com, github.com,
    # codeload.github.com and *.githubusercontent.com, no path limit. A sandbox of this
    # caller's can therefore act as that bot across the whole GitHub REST API and git protocol
    # -- not just clone/push -- by the operator's explicit choice, the same shape of tradeoff
    # `forgejo-haku` above already accepts for Forgejo.
    # `github-actions-logs` presents nothing either: GET-only to the Azure Blob Storage hosts
    # a workflow run's job logs and artifacts 302 to, so a sandbox of this caller's can follow
    # that redirect and read its own PR's CI output (egress.py).
    # `buildbuddy` presents the shared BuildBuddy API key as the literal `x-buildbuddy-api-key`
    # header/metadata value, letting this caller's sandboxes run `bazel --config=rbe` directly
    # against BuildBuddy's remote cache and Remote Execution instead of needing bbr/BuildBuddy
    # CLI tooling this namespace otherwise lacks (egress_staging_credentials.py).
    #
    # TODO: `forgejo-haku` and `haku-mailbox` stay bound here for now even though haku-agent
    # (below) exists specifically to run in-cluster Haku-like sandboxes: claude-ai is still the
    # identity the same *logical* Haku agent runs as from Claude.ai/Claude Code's own
    # out-of-cluster infrastructure, so it still needs Haku's credentials. Revisit whether
    # claude-ai should keep carrying them once haku-agent has been in use for a while.
    _account_binding(
        scope,
        claude_ai,
        policies=[
            BASIC_POLICY,
            INFERENCE_EXPERIMENTS_POLICY,
            FORGEJO_HAKU_POLICY,
            PACKAGES_POLICY,
            GOOGLE_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AIQUOTA_READ_POLICY,
            HAKU_MAILBOX_POLICY,
            COINBASE_POLICY,
            PLAID_PGWEB_POLICY,
            AGENTPLANE_TESTING_POLICY,
            _GITHUB_DOWNLOADS_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
            BUILDBUDDY_POLICY,
            *_ACCOUNT_READ_SETS,
        ],
    )

    # Haku-like managed sandboxes keep their Haku-specific access, but do not get Coinbase
    # egress or the key until a supported managed-runner identity/credential path exists (#8596).
    _account_binding(
        scope,
        haku_agent,
        policies=[
            BASIC_POLICY,
            INFERENCE_EXPERIMENTS_POLICY,
            FORGEJO_HAKU_POLICY,
            PACKAGES_POLICY,
            GOOGLE_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AIQUOTA_READ_POLICY,
            HAKU_MAILBOX_POLICY,
            PLAID_PGWEB_POLICY,
            AGENTPLANE_TESTING_POLICY,
            _GITHUB_DOWNLOADS_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
            *_ACCOUNT_READ_SETS,
        ],
    )

    # Every sandbox Action, auto-approved. Approving each one individually would not be a
    # boundary: a sandbox runs as the account that asked for it, so the whole group can do
    # exactly what that account can already do, and `exec` inside it is not narrower than
    # `provision` of it. What decides whether this is safe for an account is that account's own
    # egress and Kubernetes authority, which is why this set is bound per caller and not by default.
    _policy_set(
        scope,
        "sandbox-self",
        metadata=ApiObjectMetadata(
            name=SANDBOX_SELF_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Auto-approves every sandbox Action: a caller's own boxes, which act only as that caller, "
                    "and reads of the templates they are made from."
                )
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={SANDBOX_GROUP: sorted(SandboxAction)})],
    )

    # Home Assistant's read-only surface (see _HOME_ASSISTANT_READS_ACTIONS above for the
    # reviewed tool list and exclusions).
    _policy_set(
        scope,
        "home-assistant-reads",
        metadata=ApiObjectMetadata(
            name=HOME_ASSISTANT_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of Home Assistant MCP's default catalog, and the read-only REST routes the egress proxy presents Home Assistant's reader token on; every other Home Assistant Action stays on the human path."
            },
        ),
        rules=[
            AutoApproveIf.exact_actions(actions={"home_assistant": _HOME_ASSISTANT_READS_ACTIONS}),
            *home_assistant_read_egress_rules(),
        ],
    )

    # Gmail's and Google Calendar's read-only surfaces (see the _GMAIL_READS_ACTIONS /
    # _GOOGLE_CALENDAR_READS_ACTIONS lists above for the reviewed tool lists and exclusions).
    _policy_set(
        scope,
        "gmail-reads",
        metadata=ApiObjectMetadata(
            name=GMAIL_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Gmail MCP backend's catalog; every write stays on the human path."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"gmail": _GMAIL_READS_ACTIONS})],
    )
    _policy_set(
        scope,
        "google-calendar-reads",
        metadata=ApiObjectMetadata(
            name=GOOGLE_CALENDAR_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Google Calendar MCP backend's catalog; create_event stays on the human path."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"google_calendar": _GOOGLE_CALENDAR_READS_ACTIONS})],
    )

    _policy_set(
        scope,
        "tana-reads",
        metadata=ApiObjectMetadata(
            name=TANA_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Tana MCP backend's catalog, plus get_or_create_calendar_node; every other write and open_node stay on the human path."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"tana": _TANA_READS_ACTIONS})],
    )
    _policy_set(
        scope,
        "grocy-sf-reads",
        metadata=ApiObjectMetadata(
            name=GROCY_SF_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "The reviewed read-only subset of the Grocy SF MCP backend's catalog, and the GET-only REST routes the egress proxy presents Grocy's service-account password on; every write, open_product_stock included, stays on the human path."
            },
        ),
        rules=[
            AutoApproveIf.exact_actions(actions={"grocy_sf": _GROCY_SF_READS_ACTIONS}),
            *grocy_sf_read_egress_rules(),
        ],
    )

    # SSH MCP's `list_targets`: it names which machine/user targets are configured and whether
    # each one's local key is present -- no hostname is dialed, no command runs, and no secret
    # material is returned. Every other `ssh` Action (running a command against a target) stays
    # on the human path.
    _policy_set(
        scope,
        "ssh-reads",
        metadata=ApiObjectMetadata(
            name=SSH_READS_SET,
            namespace=_NAMESPACE,
            annotations={
                "description": "Auto-approves the SSH MCP backend's list_targets, which only names configured targets and key availability; every other SSH Action stays on the human path."
            },
        ),
        rules=[AutoApproveIf.exact_actions(actions={"ssh": ["list_targets"]})],
    )

    # The long-running OpenClaw Pod is hosted in its own namespace; keep its Action
    # authorization here with the shared policy sets, addressed by the full SA identity.
    _binding(
        scope,
        "policybinding-public-coder-openclaw-actions",
        metadata=ApiObjectMetadata(
            name="public-coder-openclaw-actions",
            namespace=_NAMESPACE,
            annotations={
                "description": "Auto-approves the reviewed common GitHub reads for the OpenClaw ServiceAccount."
            },
        ),
        subject=PolicyBindingSpecSubject(namespace=NAMESPACE, name=SERVICE_ACCOUNT),
        policy_sets=[COMMON_GITHUB_READ_SET],
    )
