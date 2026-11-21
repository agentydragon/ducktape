locals {
  oidc_property_mappings = {
    email_profile = [
      data.authentik_property_mapping_provider_scope.openid.id,
      data.authentik_property_mapping_provider_scope.email.id,
      data.authentik_property_mapping_provider_scope.profile.id,
    ]
    profile_email = [
      data.authentik_property_mapping_provider_scope.openid.id,
      data.authentik_property_mapping_provider_scope.profile.id,
      data.authentik_property_mapping_provider_scope.email.id,
    ]
    email_profile_offline_access = [
      data.authentik_property_mapping_provider_scope.openid.id,
      data.authentik_property_mapping_provider_scope.email.id,
      data.authentik_property_mapping_provider_scope.profile.id,
      data.authentik_property_mapping_provider_scope.offline_access.id,
    ]
    profile_offline_access = [
      data.authentik_property_mapping_provider_scope.openid.id,
      data.authentik_property_mapping_provider_scope.profile.id,
      data.authentik_property_mapping_provider_scope.offline_access.id,
    ]
    profile = [
      data.authentik_property_mapping_provider_scope.openid.id,
      data.authentik_property_mapping_provider_scope.profile.id,
    ]
    openid = [data.authentik_property_mapping_provider_scope.openid.id]
  }

  # Keys match the former resource labels so moved blocks can retain provider state.
  oauth2_providers = {
    agentplane_mcp = {
      name              = "agentplane-staging-mcp"
      client_id         = "agentplane-staging-mcp"
      sub_mode          = "user_uuid"
      property_mappings = local.oidc_property_mappings.email_profile_offline_access
      callback_urls     = ["${local.agentplane_mcp_origin}/auth/callback"]
    }
    agentplane_staging = {
      name      = "agentplane-staging-oauth2"
      client_id = "agentplane-staging"
      # Both the login and Action providers emit the same managed user uid, so the
      # exchange preserves identity without a provider-specific subject mapping.
      sub_mode = "hashed_user_id"
      # The profile scope supplies display names only; authorization uses issuer + sub.
      # offline_access issues the refresh token that renews the short-lived access token the app's
      # Action federation presents, for as long as the app's own session lasts.
      property_mappings = local.oidc_property_mappings.profile_offline_access
      callback_urls     = ["https://agentplane-staging.allegedly.works/auth/callback"]
    }
    ai_session_archive = {
      name                  = "ai-session-archive-oidc"
      client_id             = "ai-session-archive"
      grant_types           = ["authorization_code"]
      sub_mode              = "user_id"
      access_token_validity = "hours=1"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://ai-session-archive.allegedly.works/auth/callback"]
    }
    airlock = {
      name                  = "airlock-server"
      client_id             = "airlock-server"
      grant_types           = ["authorization_code"]
      access_token_validity = "hours=24"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://airlock.allegedly.works/auth/callback"]
    }
    claude_session_sync = {
      name        = "claude-session-sync-oidc"
      client_id   = "claude-session-sync"
      grant_types = ["authorization_code"]
      # The app checks this same immutable numeric user ID as its allowed subject; see the OIDC Secret
      # in reflected_oidc_secrets.tf.
      sub_mode              = "user_id"
      access_token_validity = "hours=1"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://claude-session-sync.allegedly.works/auth/callback"]
    }
    cli_proxy_api_admin = {
      name                  = "cli-proxy-api-admin-oidc"
      client_id             = "cli-proxy-api-admin"
      sub_mode              = "user_id"
      access_token_validity = "hours=1"
      property_mappings     = local.oidc_property_mappings.openid
      # Keep the running v7 pod's callback valid until the v8 image has rolled out.
      # Remove this entry after that rollout; the v8 backend does not serve this path.
      callback_urls = [
        "https://cli-proxy-api-admin.allegedly.works/v0/management/callback",
        "https://cli-proxy-api-admin.allegedly.works/v8/management/auth/oidc/callback",
      ]
    }
    gitlab_spike = {
      name          = "gitlab-spike-oauth2"
      client_id     = "gitlab-spike"
      grant_types   = ["authorization_code"]
      sub_mode      = "hashed_user_id"
      callback_urls = ["https://gitlab-spike.allegedly.works/users/auth/openid_connect/callback"]
    }
    headlamp = {
      name      = "headlamp"
      client_id = "headlamp"
      # Authentik defaults to 10-minute access tokens; Headlamp refreshes them on API requests.
      access_token_validity = "hours=24"
      property_mappings     = local.oidc_property_mappings.email_profile_offline_access
      callback_urls         = ["https://headlamp.allegedly.works/oidc-callback"]
    }
    home_assistant = {
      name          = "home-assistant-oauth2"
      client_id     = "home-assistant"
      client_type   = "public"
      callback_urls = ["https://home.allegedly.works/auth/oidc/callback"]
    }
    plaid_link = {
      name                  = "plaid-link-oidc"
      client_id             = "plaid-link"
      grant_types           = ["authorization_code"]
      access_token_validity = "hours=24"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://plaid-mcp.allegedly.works/auth/callback"]
    }
    plaid_spend_desktop = {
      name                  = "plaid-spend-desktop-oauth2"
      client_id             = "plaid-spend-desktop"
      client_type           = "public"
      grant_types           = ["authorization_code", "refresh_token"]
      sub_mode              = "user_uuid"
      access_token_validity = "minutes=15"
      property_mappings     = local.oidc_property_mappings.profile_offline_access
      callback_urls         = ["http://127.0.0.1:43821/callback"]
    }
    plaid_spend_web = {
      name                  = "plaid-spend-web-oidc"
      client_id             = "plaid-spend-web"
      grant_types           = ["authorization_code"]
      access_token_validity = "hours=24"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://plaid-spend.allegedly.works/auth/callback"]
    }
    quest_diagnostics = {
      name                  = "quest-diagnostics-oidc"
      client_id             = "quest-diagnostics"
      grant_types           = ["authorization_code"]
      sub_mode              = "user_id"
      access_token_validity = "hours=1"
      property_mappings     = local.oidc_property_mappings.profile_email
      callback_urls         = ["https://quest.allegedly.works/auth/callback"]
    }
    study_casino = {
      name      = "study-casino"
      client_id = "study-casino"
      # confidential — the backend does the code exchange, client_secret stays server-side
      access_token_validity = "hours=1"
      property_mappings     = local.oidc_property_mappings.profile
      callback_urls         = ["https://casino.allegedly.works/auth/callback"]
    }
  }
}

resource "authentik_provider_oauth2" "providers" {
  for_each = local.oauth2_providers

  name                       = each.value.name
  client_id                  = each.value.client_id
  client_type                = try(each.value.client_type, "confidential")
  grant_types                = try(each.value.grant_types, null)
  authorization_flow         = data.authentik_flow.implicit_consent.id
  invalidation_flow          = data.authentik_flow.invalidation.id
  signing_key                = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity      = try(each.value.access_token_validity, null)
  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  sub_mode                   = try(each.value.sub_mode, null)
  property_mappings          = try(each.value.property_mappings, local.oidc_property_mappings.email_profile)
  allowed_redirect_uris = [
    for url in each.value.callback_urls : {
      matching_mode     = "strict"
      url               = url
      redirect_uri_type = "authorization"
    }
  ]
}

moved {
  from = authentik_provider_oauth2.agentplane_mcp
  to   = authentik_provider_oauth2.providers["agentplane_mcp"]
}

moved {
  from = authentik_provider_oauth2.agentplane_staging
  to   = authentik_provider_oauth2.providers["agentplane_staging"]
}

moved {
  from = authentik_provider_oauth2.ai_session_archive
  to   = authentik_provider_oauth2.providers["ai_session_archive"]
}

moved {
  from = authentik_provider_oauth2.airlock
  to   = authentik_provider_oauth2.providers["airlock"]
}

moved {
  from = authentik_provider_oauth2.claude_session_sync
  to   = authentik_provider_oauth2.providers["claude_session_sync"]
}

moved {
  from = authentik_provider_oauth2.cli_proxy_api_admin
  to   = authentik_provider_oauth2.providers["cli_proxy_api_admin"]
}

moved {
  from = authentik_provider_oauth2.gitlab_spike
  to   = authentik_provider_oauth2.providers["gitlab_spike"]
}

moved {
  from = authentik_provider_oauth2.headlamp
  to   = authentik_provider_oauth2.providers["headlamp"]
}

moved {
  from = authentik_provider_oauth2.home_assistant
  to   = authentik_provider_oauth2.providers["home_assistant"]
}

moved {
  from = authentik_provider_oauth2.plaid_link
  to   = authentik_provider_oauth2.providers["plaid_link"]
}

moved {
  from = authentik_provider_oauth2.plaid_spend_desktop
  to   = authentik_provider_oauth2.providers["plaid_spend_desktop"]
}

moved {
  from = authentik_provider_oauth2.plaid_spend_web
  to   = authentik_provider_oauth2.providers["plaid_spend_web"]
}

moved {
  from = authentik_provider_oauth2.quest_diagnostics
  to   = authentik_provider_oauth2.providers["quest_diagnostics"]
}

moved {
  from = authentik_provider_oauth2.study_casino
  to   = authentik_provider_oauth2.providers["study_casino"]
}
