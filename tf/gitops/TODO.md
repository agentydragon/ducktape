# GitOps Terraform TODOs

- [ ] Now that the Authentik provider is on 2026.8, audit every
      `authentik_provider_oauth2`: declare the narrowest explicit `grant_types` set
      for each consumer instead of relying on Authentik defaults, then validate the
      resulting plan and live token flows.
- [ ] Audit internal-only `authentik_application` resources for
      `meta_hide = true` now that the 2026.8 provider is active. Start with the
      plumbing applications marked at their Terraform definitions; keep user-facing
      launcher entries visible and verify that hiding changes only launcher
      presentation.
