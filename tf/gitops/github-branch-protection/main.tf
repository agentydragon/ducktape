# Branch protection for agentydragon/ducktape and agentydragon/gaffer-private
# default branches. Two different mechanisms:
#
# - ducktape (public repo) → `github_repository_ruleset` on `refs/heads/main`.
#   Rulesets are GitHub's modern API, support Integration-actor bypass, and
#   are available on public repos for free. The ducktape-automation App is
#   wired in as a bypass actor so direct-push workflows that mint
#   installation tokens (sync-pins, nix-flake-update, pin-digests) can keep
#   working after ducktape's default branch flips from devel to main. Today
#   main is not yet default and the ruleset is a no-op.
#
# - gaffer-private (private repo, Free plan) → classic
#   `github_branch_protection` on `main`. Rulesets are not available on
#   Free private repos (POST returns 403 "Upgrade to GitHub Pro"). Classic
#   protection works but has weaker semantics on Free — see the comment on
#   the gaffer_main resource and plans/branch_protection.md.
#
# Auth uses the per-repo PATs already present in flux-system:
#   - github-secrets-sync-pat (Administration:R/W on ducktape; deployed
#     by cluster/k8s/github-secrets-sync/secrets/)
#   - github-pat-gaffer-private-flux (Administration:R/W on gaffer-private;
#     deployed by cluster/k8s/gaffer-private-source/)

provider "github" {
  alias = "ducktape"
  owner = "agentydragon"
  token = data.kubernetes_secret.ducktape_pat.data["token"]
}

provider "github" {
  alias = "gaffer"
  owner = "agentydragon"
  token = data.kubernetes_secret.gaffer_pat.data["token"]
}

data "kubernetes_secret" "ducktape_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "gaffer_pat" {
  metadata {
    name      = "github-pat-gaffer-private-flux"
    namespace = "flux-system"
  }
}

locals {
  # GitHub built-in "Admin" RepositoryRole — covers in-cluster
  # automations that push as the owner via PAT (Flux ImageUpdateAutomation,
  # claude-token-rotation CronJob). Constant across all repos.
  admin_repo_role_id = 5

  # ducktape-automation GitHub App; see secrets/ducktape_automation.README.md.
  # Used as the Integration bypass actor so workflows authenticated via
  # actions/create-github-app-token bypass the rulesets — github-actions
  # cannot be a bypass actor on personal-account repos, which is why we
  # registered a dedicated App.
  automation_app_id = 3590331

  ruleset_rules_common = {
    # Block branch deletion + force-push + non-linear merge commits.
    deletion                = true
    non_fast_forward        = true
    required_linear_history = true
  }
}

# --- ducktape main ---
resource "github_repository_ruleset" "ducktape_main" {
  provider    = github.ducktape
  name        = "main-protection"
  repository  = "ducktape"
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/heads/main"]
      exclude = []
    }
  }

  bypass_actors {
    actor_id    = local.admin_repo_role_id
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  bypass_actors {
    actor_id    = local.automation_app_id
    actor_type  = "Integration"
    bypass_mode = "always"
  }

  rules {
    deletion                = local.ruleset_rules_common.deletion
    non_fast_forward        = local.ruleset_rules_common.non_fast_forward
    required_linear_history = local.ruleset_rules_common.required_linear_history

    pull_request {
      # Solo repo — no review gate, just force commits through PRs so
      # required_status_checks gets a chance to run.
      required_approving_review_count = 0
    }

    required_status_checks {
      # ducktape's bazel-ci is a reusable workflow invoked from ci.yml's
      # `bazel-ci:` job, so the check name is `<caller_job_id> /
      # <called_job_name>` = `bazel-ci / Test & Build`.
      required_check {
        context = "bazel-ci / Test & Build"
      }
      required_check {
        context = "Pre-commit checks"
      }
      strict_required_status_checks_policy = false
    }
  }
}

# --- gaffer-private main ---
#
# Classic branch protection (not a ruleset) because rulesets are not
# available on Free private repos. See plans/branch_protection.md for the
# full trade-off; the short version:
#
# What this gives us:
#   - Block deletion + force-push + non-linear merges
#   - PR merges via the merge button gated on `Test & Build` and
#     `Pre-commit checks` passing
#
# What this does NOT give us (gap vs ducktape's ruleset):
#   - Direct `git push` from any actor with `Contents:write` is NOT gated
#     on the CI checks. Classic protection's required_status_checks only
#     enforces on PR merge buttons; pushes to the branch ref bypass it.
#     This means a contributor (or an in-cluster automation) could shove
#     a red commit straight to main without going through a PR.
#
# TODO: close that gap once one of these becomes feasible:
#   1. Upgrade `agentydragon` to GitHub Pro (~$4/mo) → switch this resource
#      back to a ruleset with bypass_actors{Integration=ducktape-automation}
#      mirroring the ducktape side.
#   2. Migrate Flux's gaffer-images ImageUpdateAutomation off direct
#      pushes onto a PR-based flow (push to a feature branch + auto-open +
#      auto-merge a PR). With direct pushes gone, classic protection's
#      `required_pull_request_reviews` becomes safe to enable, which would
#      block all non-PR pushes.
#
# TODO: enable GitHub secret-scanning push protection on gaffer-private.
# Independent of branch protection — push protection blocks pushes that
# contain secrets at the moment of `git push`. Free for public repos; on
# private repos it requires GitHub Advanced Security, but worth checking
# whether the personal-account "Secret Protection" SKU covers this.
resource "github_branch_protection" "gaffer_main" {
  #checkov:skip=CKV_GIT_5:Solo repo. There is no second reviewer to require.
  #checkov:skip=CKV_GIT_6:Signed-commit enforcement is a separate decision; not adopting it across the board today.
  provider      = github.gaffer
  repository_id = "gaffer-private"
  pattern       = "main"

  enforce_admins          = false
  required_linear_history = true
  allows_deletions        = false
  allows_force_pushes     = false

  required_status_checks {
    strict = false
    # gaffer's bazel-ci and pre-commit are both top-level workflows (not
    # invoked via workflow_call), so check_runs.name is just the job name
    # in each case. Empirically verified on PRs #16/#17.
    contexts = [
      "Test & Build",
      "Pre-commit checks",
    ]
  }

  # Intentionally NO required_pull_request_reviews — that block makes PRs
  # mandatory for ALL writes, including Flux's gaffer-images
  # ImageUpdateAutomation, which pushes commits directly to main. We'd
  # need the Pro-only `restrict_pushes` to whitelist the App, or migrate
  # Flux off direct pushes; see TODOs above.
}
