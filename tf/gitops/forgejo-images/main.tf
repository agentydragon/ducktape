# Forgejo registry tenant for ducktape CI-built in-cluster images.
#
# Creates a dedicated `ducktape-ci` Forgejo user that owns CI-pushed images
# (git.allegedly.works/ducktape-ci/<image>, e.g. codex-pod). Its password is the
# shared value in the SOPS-provisioned `forgejo-images-creds` Secret, so CI push
# (secrets/ci/forgejo-images-registry.sops.yaml) and the kubelet/Flux pull
# credential (the same Secret, reflected into flux-system + consuming namespaces)
# all authenticate as this user. Deliberately no proxy (unlike props): clients
# talk to Forgejo directly.

data "kubernetes_secret" "forgejo_admin" {
  metadata {
    name      = "forgejo-admin-password"
    namespace = "forgejo"
  }
}

data "kubernetes_secret" "images_creds" {
  metadata {
    name      = "forgejo-images-creds"
    namespace = "forgejo-images"
  }
}

provider "forgejo" {
  host     = var.forgejo_url
  username = data.kubernetes_secret.forgejo_admin.data["username"]
  password = data.kubernetes_secret.forgejo_admin.data["password"]
}

resource "forgejo_user" "images" {
  login                = "ducktape-ci"
  email                = "ducktape-ci@allegedly.works"
  password             = data.kubernetes_secret.images_creds.data["password"]
  must_change_password = false
  visibility           = "private"
}
