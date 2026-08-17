# Shared Authentik users referenced by multiple access-control resources.
# Keep these independent of any one application so retiring an application
# cannot remove data sources still required by the rest of this module.
data "authentik_user" "agentydragon" {
  username = "agentydragon"
}

data "authentik_user" "auragon" {
  username = "auragon"
}
