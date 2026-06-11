# Forgejo registry tenant for props.
#
# Creates a dedicated `props` service user that owns the props agent images
# (git.allegedly.works/props/{critic,grader,critic_dev,...}). The props registry
# proxy authenticates as this user to forward pushes/pulls to Forgejo.
#
# This is proxy-to-Forgejo auth only. Clients of the props registry proxy
# (CI pushers and kubelet pulls) authenticate to the proxy with props Postgres
# credentials; the kubelet dockerconfigjson Secret is rendered by
# cluster/k8s/props/app/registry-pull-secret.yaml from the CNPG props-db-app
# Secret.

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

# Upstream creds for the props registry proxy (Basic auth to Forgejo).
resource "kubernetes_secret" "props_forgejo_upstream_creds" {
  metadata {
    name      = "props-forgejo-upstream-creds"
    namespace = "props"
  }

  data = {
    username = forgejo_user.props.login
    password = random_password.props.result
  }
}
