"""Staging-only Action Service policy objects: the claude-ai caller ServiceAccount, its
five reviewed GitHub-reads ActionPolicySets, and the ActionPolicyBinding granting them to
that ServiceAccount. See cluster/k8s/agentplane-staging/README.md § Action policies --
Sandbox-subject bindings are written by the integration app at runtime and are never
checked in here.
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
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.agentplane.app_settings import BASIC_POLICY, FORGEJO_HAKU_POLICY, KUBERNETES_POLICY, PACKAGES_POLICY
from cluster.cdk8s.agentplane.staging_config import (
    PUBLIC_DUCKTAPE_FORK_READS_SET,
    PUBLIC_DUCKTAPE_READS_SET,
    PUBLIC_GAFFER_PRIVATE_READS_SET,
    PUBLIC_GITHUB_READS_SET,
)
from agentplane.action_service.policies.resources import BindingSpec, PolicySetSpec
from agentplane.action_service.sandbox_executor import SANDBOX_GROUP, SandboxAction

_NAMESPACE = "agentplane-staging"
_GITHUB_READS_SET = "github-reads"
_SANDBOX_SET = "sandbox-self"
_GITHUB_IDENTITY_READS_SET = "github-identity-reads"


def _policy_set(scope: Construct, id: str, *, metadata: ApiObjectMetadata, spec: ActionPolicySetSpec) -> None:
    # The Action Service parses `spec` more strictly than the CRD schema (an unknown policy
    # kind or key, an invalid JSON Schema); an object it refuses reports Ready=False on the
    # cluster and contributes nothing, so it fails here instead.
    PolicySetSpec.model_validate(ActionPolicySet(scope, id, metadata=metadata, spec=spec).to_json()["spec"])


def _binding(scope: Construct, id: str, *, metadata: ApiObjectMetadata, spec: ActionPolicyBindingSpec) -> None:
    BindingSpec.model_validate(ActionPolicyBinding(scope, id, metadata=metadata, spec=spec).to_json()["spec"])


# The console's `github_reads` policy (cluster/cdk8s/haku/console_config.py) as an Action
# policy set. GitHub MCP's normal endpoint exposes its default catalog, including writes.
# This is the explicit 2026-08-14 upstream read-only subset: new upstream tools
# intentionally stay manual until reviewed here. `ui_get` reads an MCP App UI resource,
# not GitHub repository state.
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
    # The principal a Connection enrolled from the Claude.ai MCP connector acts as,
    # picked by the operator at OAuth consent. No Pod runs as it and it holds no
    # RoleBinding: the label is what makes it an Action caller, and removing the label
    # or the object is how it is disabled. What it may do without an operator is an
    # ActionPolicyBinding naming it; on its own it grants nothing.
    ServiceAccount(
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

    # What the console's `haku_v1` grants for GitHub (github-reads and
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
    #
    # TODO(github-egress): consider binding `github-public` here too. The asymmetry today is that
    # the ActionPolicyBinding below auto-approves GitHub *reads through the Action Service*, while
    # a sandbox of the same caller cannot reach github.com at all -- so `git clone` fails in a box
    # whose caller can read the same repository through an Action. Two things to settle first: the
    # policy substitutes the `agentydragon-agent` PAT, which is write-capable, on GET and POST with
    # no path limit, so binding it lets a sandbox push as that bot; and the policy omits
    # `codeload.github.com`, where a `github.com/.../archive/...` fetch actually lands, so Bazel and
    # tarball downloads would still fail until that host joins it.
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
            policies=[BASIC_POLICY, KUBERNETES_POLICY, FORGEJO_HAKU_POLICY, PACKAGES_POLICY],
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

    # github-identity-reads), attached to the Claude.ai connector's principal. The
    # binding's existence is the grant: deleting it, or the label on the
    # ServiceAccount, puts every GitHub Action back on the human path.
    _binding(
        scope,
        "actionpolicybinding-claude-ai-github-reads",
        metadata=ApiObjectMetadata(
            name="claude-ai-github-reads",
            namespace=_NAMESPACE,
            annotations={
                "description": "Auto-approves the reviewed GitHub reads and sandbox use for Connections acting as the claude-ai ServiceAccount."
            },
        ),
        spec=ActionPolicyBindingSpec(
            subject=ActionPolicyBindingSpecSubject(namespace=_NAMESPACE, name="claude-ai"),
            policy_sets=[_GITHUB_READS_SET, _GITHUB_IDENTITY_READS_SET, _SANDBOX_SET],
        ),
    )
