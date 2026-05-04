# Branch protection for agentydragon/ducktape `main`.
#
# Implemented as `github_repository_ruleset` on `refs/heads/main`. Rulesets
# are GitHub's modern API and support Integration-actor bypass — the
# ducktape-automation App is wired in below so direct-push workflows that
# mint installation tokens (sync-pins, nix-flake-update, pin-digests) keep
# working after ducktape's default branch flips from devel to main. main
# is not yet default; the ruleset enforces nothing today.
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

resource "github_repository_ruleset" "ducktape_main" {
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
