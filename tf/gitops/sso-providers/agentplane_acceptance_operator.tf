# Dedicated staging test operator, not a workload/service-account identity.
# The password is generated only on apply and retained in protected TF state.
resource "random_password" "agentplane_acceptance_operator" {
  length  = 64
  special = false
}

resource "authentik_user" "agentplane_acceptance_operator" {
  username = "agentplane-acceptance-operator"
  name     = "Agentplane staging acceptance operator"
  type     = "internal"
  password = random_password.agentplane_acceptance_operator.result
  groups   = []
  roles    = []
}

# The user resource exposes only the numeric primary key, not uid/uuid.
# Resolve the newly managed account via the same API lookup used for Rai.
data "authentik_user" "agentplane_acceptance_operator" {
  pk = tonumber(authentik_user.agentplane_acceptance_operator.id)
}

resource "authentik_policy_binding" "agentplane_acceptance_staging_access" {
  target = authentik_application.agentplane_staging.uuid
  user   = tonumber(authentik_user.agentplane_acceptance_operator.id)
  order  = 1
}

resource "authentik_policy_binding" "agentplane_acceptance_actions_access" {
  target = authentik_application.agentplane_actions.uuid
  user   = tonumber(authentik_user.agentplane_acceptance_operator.id)
  order  = 1
}

# Read on demand by the acceptance runner through Console-authorized Kubernetes
# GET, not mounted in OpenClaw, the proxy, or a sandbox. No pre-issued session.
resource "kubernetes_secret" "agentplane_acceptance_operator" {
  metadata {
    name      = "agentplane-acceptance-operator"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "public-coder-agent"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "public-coder-agent"
    }
  }
  data = {
    login    = authentik_user.agentplane_acceptance_operator.username
    username = authentik_user.agentplane_acceptance_operator.username
    password = random_password.agentplane_acceptance_operator.result
    issuer   = local.agentplane_actions_issuer
    subject  = data.authentik_user.agentplane_acceptance_operator.uuid
  }
  lifecycle {
    precondition {
      condition     = data.authentik_user.agentplane_acceptance_operator.uid != "" && data.authentik_user.agentplane_acceptance_operator.uuid != ""
      error_message = "The acceptance operator must have authoritative Authentik uid and uuid values."
    }
  }
}
