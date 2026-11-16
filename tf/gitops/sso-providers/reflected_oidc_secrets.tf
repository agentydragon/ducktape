locals {
  reflected_oidc_secret_metadata = {
    ai_session_archive = {
      name               = "ai-session-archive-oidc"
      allowed_namespaces = "session-backup"
    }
    claude_session_sync = {
      name               = "claude-session-sync-oidc"
      allowed_namespaces = "claude-session-sync"
    }
    cli_proxy_api_admin = {
      name               = "cli-proxy-api-admin-oidc"
      allowed_namespaces = "cli-proxy-api"
    }
    forgejo = {
      name               = "forgejo-oauth-client-secret"
      allowed_namespaces = "forgejo,flux-system"
    }
    grafana = {
      name               = "grafana-oidc-config"
      allowed_namespaces = "monitoring"
    }
    langfuse = {
      name               = "langfuse-oidc-config"
      allowed_namespaces = "langfuse"
    }
    matrix = {
      name               = "matrix-oidc-config"
      allowed_namespaces = "matrix"
    }
    props = {
      name               = "props-oidc-config"
      allowed_namespaces = "props"
    }
  }

  reflected_oidc_secret_data = {
    # The application pins the numeric subject too, preserving owner-only authorization if the
    # Authentik policy changes. These keys are consumed by the AI session archive deployment.
    ai_session_archive = {
      AI_SESSION_ARCHIVE_OIDC_ISSUER          = "https://auth.allegedly.works/application/o/ai-session-archive/"
      AI_SESSION_ARCHIVE_OIDC_CLIENT_ID       = authentik_provider_oauth2.ai_session_archive.client_id
      AI_SESSION_ARCHIVE_OIDC_CLIENT_SECRET   = authentik_provider_oauth2.ai_session_archive.client_secret
      AI_SESSION_ARCHIVE_OIDC_SESSION_SECRET  = random_password.ai_session_archive_session_secret.result
      AI_SESSION_ARCHIVE_OIDC_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
    }
    # The session sync Deployment reads these keys as the web app's `WebSettings` env vars.
    claude_session_sync = {
      SESSION_SYNC_OIDC_ISSUER          = "https://auth.allegedly.works/application/o/claude-session-sync/"
      SESSION_SYNC_OIDC_CLIENT_ID       = authentik_provider_oauth2.claude_session_sync.client_id
      SESSION_SYNC_OIDC_CLIENT_SECRET   = authentik_provider_oauth2.claude_session_sync.client_secret
      SESSION_SYNC_OIDC_SESSION_SECRET  = random_password.claude_session_sync_session_secret.result
      SESSION_SYNC_OIDC_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
    }
    cli_proxy_api_admin = {
      MANAGEMENT_OIDC_ISSUER           = "https://auth.allegedly.works/application/o/cli-proxy-api-admin/"
      MANAGEMENT_OIDC_CLIENT_ID        = authentik_provider_oauth2.cli_proxy_api_admin.client_id
      MANAGEMENT_OIDC_CLIENT_SECRET    = authentik_provider_oauth2.cli_proxy_api_admin.client_secret
      MANAGEMENT_OIDC_REDIRECT_URL     = "https://cli-proxy-api-admin.allegedly.works/v8/management/auth/oidc/callback"
      MANAGEMENT_OIDC_ALLOWED_SUBJECTS = tostring(authentik_user.agentydragon.id)
    }
    forgejo = {
      # The Forgejo chart expects `key` for client ID and `secret` for client secret.
      key    = authentik_provider_oauth2.confidential["forgejo"].client_id
      secret = authentik_provider_oauth2.confidential["forgejo"].client_secret
    }
    grafana = {
      GF_AUTH_GENERIC_OAUTH_CLIENT_ID     = authentik_provider_oauth2.confidential["grafana"].client_id
      GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET = authentik_provider_oauth2.confidential["grafana"].client_secret
    }
    langfuse = {
      "client-id"     = authentik_provider_oauth2.confidential["langfuse"].client_id
      "client-secret" = authentik_provider_oauth2.confidential["langfuse"].client_secret
    }
    # Synapse consumes this Secret through HelmRelease `valuesFrom`.
    matrix = {
      # The {{ }} templates are Synapse Jinja2, not HCL interpolation.
      "values.yaml" = <<-EOT
        extraConfig:
          oidc_providers:
            - idp_id: authentik
              idp_name: "Authentik SSO"
              issuer: "https://auth.allegedly.works/application/o/matrix/"
              client_id: "${authentik_provider_oauth2.confidential["matrix"].client_id}"
              client_secret: "${authentik_provider_oauth2.confidential["matrix"].client_secret}"
              discover: true
              scopes:
                - openid
                - profile
                - email
              allow_existing_users: true
              enable_registration: true
              user_mapping_provider:
                config:
                  localpart_template: "{{ user.preferred_username }}"
                  display_name_template: "{{ user.name }}"
                  email_template: "{{ user.email }}"
      EOT
    }
    props = {
      "client-id"      = authentik_provider_oauth2.confidential["props"].client_id
      "client-secret"  = authentik_provider_oauth2.confidential["props"].client_secret
      "session-secret" = random_password.props_session_secret.result
    }
  }
}

resource "kubernetes_secret" "reflected_oidc" {
  for_each = local.reflected_oidc_secret_metadata

  metadata {
    name      = each.value.name
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = each.value.allowed_namespaces
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = each.value.allowed_namespaces
    }
  }

  data = local.reflected_oidc_secret_data[each.key]
}

moved {
  from = kubernetes_secret.ai_session_archive_oidc
  to   = kubernetes_secret.reflected_oidc["ai_session_archive"]
}
moved {
  from = kubernetes_secret.claude_session_sync_oidc
  to   = kubernetes_secret.reflected_oidc["claude_session_sync"]
}
moved {
  from = kubernetes_secret.cli_proxy_api_admin_oidc
  to   = kubernetes_secret.reflected_oidc["cli_proxy_api_admin"]
}
moved {
  from = kubernetes_secret.forgejo_oauth
  to   = kubernetes_secret.reflected_oidc["forgejo"]
}
moved {
  from = kubernetes_secret.grafana_oidc
  to   = kubernetes_secret.reflected_oidc["grafana"]
}
moved {
  from = kubernetes_secret.langfuse_oidc
  to   = kubernetes_secret.reflected_oidc["langfuse"]
}
moved {
  from = kubernetes_secret.matrix_oidc
  to   = kubernetes_secret.reflected_oidc["matrix"]
}
moved {
  from = kubernetes_secret.props_oidc
  to   = kubernetes_secret.reflected_oidc["props"]
}
