# InvenTree revival

- [ ] Restore Forgejo publishing for both existing image targets in
      [the CI roster](../../../devinfra/ci/image_targets.json).
      `inventree` builds `//inventree_utils/rai_plugin:image` (entry in `c995ec223a`).
      `inventree-token-provisioner` builds `//cluster/archive/inventree_token_provisioner:image`.
      Repin the [Job](token-provisioner/job.yaml) and [CronJob](token-provisioner/cronjob.yaml)
      to the published provisioner image and restore their Flux image policy and setters.
- [ ] Supply fresh admin credentials in the SOPS-managed `inventree-admin-password` Secret.
      The old Vault values are gone. CNPG generates `inventree-db-app` for the database owner.
      [Helm values](app/helmrelease.yaml) · [CNPG bootstrap](db/postgres-cluster.yaml)
- [ ] Restore the Authentik application, its access policy, and delivery of
      `inventree-sso-providers` to the app namespace (removed in `cc6b8997d9`).
- [ ] Re-register the namespace, database, app, and token provisioner with Flux.
      `63c3cad108` removed their Kustomizations. The app consumes the database
      and SSO credentials. The provisioner needs the running app.
