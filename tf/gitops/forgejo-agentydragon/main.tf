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
  # Public keys are duplicated from ssh_keys/*-forgejo.pub because tofu-controller
  # executes only this Terraform module path, so file() cannot read repo-root files.
  public_keys = {
    atlas  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG6/fkFmt/yOWY0s+PHulmGoTh8kll0WHoL8NXWluTn/"
    iguana = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMZZ91cQfULMM/rcBcYGaJ4HSiGeD8/G140ElIToYvIV"
    rugged = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILlk0/g/0s5plFxzC8V/G8IHlTmlX+0ZYqIPr5+Tglcp"
    wyrm2  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKp26ICwuhnkiPNqhFjly2k/iW5vs7n5Rq27imnrpK/e"
  }
}

resource "forgejo_ssh_key" "host" {
  for_each = local.public_keys

  user  = data.forgejo_user.agentydragon.login
  key   = each.value
  title = "${each.key}-forgejo"
}
