# Manage SSH keys on the agentydragon GitHub account.
#
# NOTE: The "GPD Win Max 2 SSH key" (RSA, ID 90675672) exists on GitHub
# but is not managed here.
#
# Public key files include a comment field (e.g. "agentydragon@atlas-github")
# for use in authorized_keys. GitHub strips the comment on upload, so we
# extract only the key type and key data to avoid spurious replacements.

locals {
  # Read pubkey file, split into [type, key_data, comment...], rejoin first two.
  pubkey = { for name, path in {
    atlas_github   = "${path.module}/../../ssh_keys/atlas-github.pub"
    iguana_github  = "${path.module}/../../ssh_keys/iguana-github.pub"
    rugged_github  = "${path.module}/../../ssh_keys/rugged-github.pub"
    iguana_default = "${path.module}/../../ssh_keys/iguana-default.pub"
    } : name => join(" ", slice(split(" ", trimspace(file(path))), 0, 2))
  }
}

provider "github" {
  # Token from GITHUB_TOKEN env var (set via .envrc from SOPS)
}

resource "github_user_ssh_key" "atlas_github" {
  title = "atlas-github"
  key   = local.pubkey["atlas_github"]
}

resource "github_user_ssh_key" "iguana_github" {
  title = "iguana ThinkPad X1 Extreme Gen3 GitHub key"
  key   = local.pubkey["iguana_github"]
}

resource "github_user_ssh_key" "rugged_github" {
  title = "Dell Rugged 12 Tablet GitHub key"
  key   = local.pubkey["rugged_github"]
}

resource "github_user_ssh_key" "iguana_default" {
  title = "iguana/wyrm2 default key"
  key   = local.pubkey["iguana_default"]
}
