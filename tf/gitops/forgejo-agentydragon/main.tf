# Forgejo user-level SSH keys for the human `agentydragon` account.
#
# The account itself is created by normal OIDC login and is intentionally not
# managed here. This module only attaches per-host SSH public keys so git over
# SSH can authenticate as the user's Forgejo account.

data "kubernetes_secret" "forgejo_admin" {
  metadata {
    name      = "forgejo-admin-password"
    namespace = "forgejo"
  }
}

provider "forgejo" {
  host     = var.forgejo_url
  username = data.kubernetes_secret.forgejo_admin.data["username"]
  password = data.kubernetes_secret.forgejo_admin.data["password"]
}

data "forgejo_user" "agentydragon" {
  login = "agentydragon"
}

locals {
  # Forgejo stores only the key type + key data. Keep comments in the .pub
  # files for humans, but strip them here to avoid provider normalization churn.
  public_keys = {
    atlas  = "${path.module}/../../../ssh_keys/atlas-forgejo.pub"
    iguana = "${path.module}/../../../ssh_keys/iguana-forgejo.pub"
    rugged = "${path.module}/../../../ssh_keys/rugged-forgejo.pub"
    wyrm2  = "${path.module}/../../../ssh_keys/wyrm2-forgejo.pub"
  }

  pubkey = {
    for host, path in local.public_keys :
    host => join(" ", slice(split(" ", trimspace(file(path))), 0, 2))
  }
}

resource "forgejo_ssh_key" "host" {
  for_each = local.pubkey

  user  = data.forgejo_user.agentydragon.login
  key   = each.value
  title = "${each.key}-forgejo"
}
