# GitHub repository rulesets for agentydragon/ducktape
#
# Enforces PR-gated merges into devel (current default) and main (future
# default after a planned rename), with required status checks from the
# Pre-commit and Bazel CI workflows. Direct pushes are blocked for everyone
# except explicit bypass actors.
#
# Bypass actors (bypass_mode = always):
#   - Repository admin role: covers the owner (agentydragon). The fine-grained
#     PAT in `github-secrets-sync-pat` is owned by the admin user, so all
#     automations pushing via that PAT — Flux ImageUpdateAutomation and the
#     claude-token-rotation CronJob — inherit this bypass.
#   - github-actions GitHub App (id 15368): covers workflows that direct-push
#     via GITHUB_TOKEN, specifically sync-pins.yml, nix-flake-update.yml, and
#     the pin-digests job in container-images.yml.
#
# Status check contexts are written from first principles based on the
# workflow/job names in .github/workflows/. They must be verified against an
# actual PR check run and corrected if GitHub reports different strings —
# rulesets match on the literal context string, so a mismatch silently makes
# the check non-required. See the CLEANUP note below.
#
# Auth: fine-grained GitHub PAT stored as K8s Secret (SOPS-deployed by Flux).

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_secrets_sync_pat.data["token"]
}

data "kubernetes_secret" "github_secrets_sync_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

resource "github_repository_ruleset" "protect_default_branches" {
  name        = "protect-default-branches"
  repository  = "ducktape"
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      # Both devel (current default) and main (future default after rename) —
      # keeping both in the include list means protection follows the default
      # branch across a rename with no gap.
      include = ["refs/heads/devel", "refs/heads/main"]
      exclude = []
    }
  }

  # Repository admin (actor_id = 5) — covers the owner and anything pushing
  # with a PAT owned by the admin user (Flux image automation, claude token
  # rotation cronjob, manual emergencies).
  bypass_actors {
    actor_id    = 5
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  # github-actions GitHub App (well-known integration id 15368) — covers
  # workflows that direct-push via GITHUB_TOKEN.
  bypass_actors {
    actor_id    = 15368
    actor_type  = "Integration"
    bypass_mode = "always"
  }

  rules {
    # Block branch deletion and force pushes. Linear history forces
    # squash/rebase merges, matching the existing monorepo convention.
    deletion                = true
    non_fast_forward        = true
    required_linear_history = true

    pull_request {
      # Solo-maintained repo — requiring approvals would just block me.
      # The safety comes from required status checks, not review gating.
      required_approving_review_count   = 0
      required_review_thread_resolution = true
      dismiss_stale_reviews_on_push     = false
      require_code_owner_review         = false
      require_last_push_approval        = false
    }

    required_status_checks {
      # Branches must be up to date with the target before merging, so the
      # required checks reflect the post-merge state.
      strict_required_status_checks_policy = true

      # CLEANUP(2026-04-15): Verify these context strings against an actual
      # PR check run on github.com and correct if the reported names differ.
      # Reusable workflow jobs render as `<caller> / <job-id> / <job-name>`.
      required_check {
        context = "Pre-commit checks / Pre-commit checks"
      }
      required_check {
        context = "CI / bazel-ci / Test & Build"
      }
    }
  }
}
