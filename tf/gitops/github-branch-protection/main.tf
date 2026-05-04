# Branch-protection rulesets for agentydragon/ducktape and
# agentydragon/gaffer-private. Both target refs/heads/main.
#
# On ducktape, main is not currently the default branch; the ruleset
# enforces nothing today and will start gating direct pushes when the
# default flips from devel. This is the "Option C — partial protection"
# path described in plans/branch_protection.md, with the ducktape-automation
# GitHub App added as a bypass actor so the three GHA workflows that
# direct-push to the default branch (sync-pins, nix-flake-update,
# container-images:pin-digests) can keep working after migration to
# actions/create-github-app-token. See secrets/ducktape_automation.README.md.
#
# On gaffer-private, main is the default branch and the ruleset enforces
# on apply. Flux's gaffer-images ImageUpdateAutomation now pushes via the
# ducktape-automation App (see the Integration bypass actor below), so its
# image-pin commits land cleanly.
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
resource "github_repository_ruleset" "gaffer_main" {
  provider    = github.gaffer
  name        = "main-protection"
  repository  = "gaffer-private"
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
      required_approving_review_count = 0
    }

    required_status_checks {
      # gaffer's bazel-ci and pre-commit are both top-level workflows
      # (not invoked via workflow_call), so check_runs.name is just the
      # job name in each case. Empirically verified on PRs #16/#17.
      required_check {
        context = "Test & Build"
      }
      required_check {
        context = "Pre-commit checks"
      }
      strict_required_status_checks_policy = false
    }
  }
}
