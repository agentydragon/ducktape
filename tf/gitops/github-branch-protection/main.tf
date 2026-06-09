# Branch protection for agentydragon/ducktape's default branches.
#
# Implemented as a `github_repository_ruleset` on `refs/heads/devel` and
# `refs/heads/main`. Rulesets are GitHub's modern API and support
# Integration-actor bypass — the ducktape-automation App is wired in below so
# direct-push workflows that mint installation tokens (sync-pins,
# nix-flake-update, container-images pin-digests) keep working. All three
# already push as the App, so they are covered by the Integration bypass; the
# admin RepositoryRole bypass covers owner/PAT pushes (Flux image automation,
# rotation CronJobs, the owner's own pushes).
#
# Staged at `enforcement = "evaluate"`: rules are logged in the ruleset
# insights but not blocked. Flip to "active" once one PR confirms the two
# required check contexts match and the automation pushes bypass cleanly.
#
# Gaffer-private has NO branch protection from this module. GitHub Free
# does not include any branch protection (rulesets or classic) on private
# repositories. Both `github_repository_ruleset` and
# `github_branch_protection` apply attempts on gaffer-private have been
# verified to fail with `403 Upgrade to GitHub Pro or make this repository
# public to enable this feature`. Closing that gap requires a Pro upgrade
# (~$4/mo). See plans/branch_protection.md.
#
# Auth: github-secrets-sync-pat (Administration:R/W on ducktape; deployed
# by cluster/k8s/github-secrets-sync/secrets/).

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.ducktape_pat.data["token"]
}

data "kubernetes_secret" "ducktape_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

locals {
  # GitHub built-in "Admin" RepositoryRole — covers in-cluster automations
  # that push as the owner via PAT (Flux ImageUpdateAutomation,
  # claude-token-rotation, attic-jwt-rotation CronJobs).
  admin_repo_role_id = 5

  # ducktape-automation GitHub App; see secrets/ducktape_automation.README.md.
  # Used as the Integration bypass actor so workflows authenticated via
  # actions/create-github-app-token bypass the ruleset — github-actions
  # cannot be a bypass actor on personal-account repos, which is why we
  # registered a dedicated App.
  automation_app_id = 3590331
}

# CLEANUP(2026-06-09): resource renamed ducktape_main -> default_branch_protection
# when devel was added to the ruleset. Drop this moved block once the apply
# (state move) has landed in the tf-runner backend.
moved {
  from = github_repository_ruleset.ducktape_main
  to   = github_repository_ruleset.default_branch_protection
}

resource "github_repository_ruleset" "default_branch_protection" {
  name        = "default-branch-protection"
  repository  = "ducktape"
  target      = "branch"
  enforcement = "evaluate"

  conditions {
    ref_name {
      include = ["refs/heads/devel", "refs/heads/main"]
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
    # Block branch deletion + force-push + non-linear merge commits.
    deletion                = true
    non_fast_forward        = true
    required_linear_history = true

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
