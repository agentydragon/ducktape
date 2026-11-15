moved {
  from = authentik_provider_oauth2.forgejo
  to   = authentik_provider_oauth2.confidential["forgejo"]
}
moved {
  from = authentik_provider_oauth2.grafana
  to   = authentik_provider_oauth2.confidential["grafana"]
}
moved {
  from = authentik_provider_oauth2.langfuse
  to   = authentik_provider_oauth2.confidential["langfuse"]
}
moved {
  from = authentik_provider_oauth2.matrix
  to   = authentik_provider_oauth2.confidential["matrix"]
}
moved {
  from = authentik_provider_oauth2.props
  to   = authentik_provider_oauth2.confidential["props"]
}
