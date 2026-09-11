# Staging Action Service

MCP OAuth redirects in `settings.yaml` must return to the staging integration app's
`/mcp-linkage/callback`. The app forwards completion to the Action Service using the
operator's authenticated session, then returns the browser to the MCP servers page.

Kubernetes uses the public `kubectl-passthrough-mcp` client. Its exact callback allowlist
is managed in `tf/gitops/agent-machine-access/kubectl-common.tf`; that Terraform change
must reconcile before linking Kubernetes through this origin.

GitHub uses the existing GitHub App credentials reflected from
`haku-console/haku-console-github-mcp-client-credentials`. The App registration is managed
outside this repository. Its owner must include
`https://agentplane-staging.allegedly.works/mcp-linkage/callback` among the user-authorization
callback URLs, retaining Haku Console's existing callback. GitHub Apps support
[multiple callback URLs](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url).
Changing this deployment's `redirect_uri` does not update the registration.
