# Forgejo registry tenant for props.
#
# Creates a dedicated `props` service user that owns the props agent images
# (git.allegedly.works/props/{critic,grader,critic_dev,...}). The props registry
# proxy authenticates as this user to forward pushes/pulls to Forgejo.
#
# This module only owns the Forgejo-side service account. The Props namespace
# and registry proxy have been retired; their Kubernetes credentials are no
# longer managed here.

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

resource "random_password" "props" {
  length  = 48
  special = false
}

resource "forgejo_user" "props" {
  login                = "props"
  email                = "props@allegedly.works"
  password             = random_password.props.result
  must_change_password = false
  visibility           = "private"
}
