#!/usr/bin/env bash
# CI environment: common secrets only (BuildBuddy). No machine-user identity.
#
# Sourced by CI steps that need BuildBuddy credentials without the full
# web/CLI secret set.
#
# Usage: source devinfra/secrets/ci_env.sh
#
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
# ci_env.sh adds nothing beyond _common.sh today; it exists as the documented
# CI entry point and a seam for future CI-only secrets.

# Restore the caller's shell options (don't leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
