# Plan: Wire Grocy into cluster/

Deploy [Grocy](https://grocy.info/) (self-hosted groceries & household management) at
`grocy.allegedly.works`, protected by Authentik proxy outpost.

## Key decisions

**Helm chart**: No maintained dedicated Grocy chart exists — k8s-at-home is archived;
bjw-s app-template is generic overhead for a single-container app. Use **raw K8s manifests**
following the atuin pattern (Deployment + Service + PVC), which is already established in
this cluster.

**Authentik integration**: Proxy outpost pattern (mode: `proxy`), same as loki/gatus/hubble/openclaw.
No client secret needed (proxy mode; the outpost handles all auth and proxies requests to the
internal Grocy service). No Terraform secrets module needed.

**Database**: SQLite (default Grocy, no separate DB container or credentials).

**Storage**: 5 Gi PVC on `proxmox-csi-retain`, mounted at `/config`. Node-pinned to proxmox
(like atuin), with `strategy: Recreate` (RWO volume).

---

## Files to create

### Namespace

| File                                          | Content                                                |
| --------------------------------------------- | ------------------------------------------------------ |
| `k8s/grocy-namespace/namespace.yaml`          | `Namespace` named `grocy`                              |
| `k8s/grocy-namespace/kustomization.yaml`      | `resources: [namespace.yaml, flux-kustomization.yaml]` |
| `k8s/grocy-namespace/flux-kustomization.yaml` | Flux `Kustomization` `grocy-namespace`, no deps        |

### Application (`k8s/applications/grocy/`)

| File                      | Content                                                                                                                                                                                                             |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `deployment.yaml`         | `Deployment` `grocy`, image `lscr.io/linuxserver/grocy:latest`, mounts `/config` PVC, port 80, env `GROCY_BASE_URL=https://grocy.allegedly.works`, nodeSelector `proxmox`, strategy `Recreate`, reloader annotation |
| `service.yaml`            | `Service` `grocy`, ClusterIP, port 80                                                                                                                                                                               |
| `pvc.yaml`                | `PersistentVolumeClaim` `grocy-config`, 5 Gi, `proxmox-csi-retain`, `ReadWriteOnce`                                                                                                                                 |
| `kustomization.yaml`      | `resources: [deployment.yaml, service.yaml, pvc.yaml]`                                                                                                                                                              |
| `flux-kustomization.yaml` | Flux `Kustomization` `grocy`, depends on `grocy-namespace`, `gateway`, `proxmox-csi`, `cert-manager-issuer-config`, `cert-manager-environment`, `authentik`                                                         |

### Authentik proxy HTTPRoute

| File                                              | Content                                                                                                  |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `k8s/authentik-proxy-routes/grocy-httproute.yaml` | `HTTPRoute` in `authentik` ns, hostname `grocy.allegedly.works`, backend `ak-outpost-grocy-outpost:9000` |

---

## Files to modify

### `k8s/authentik/sso-blueprints.yaml`

Add a new `grocy-sso.yaml` key to the ConfigMap with these Authentik blueprint entries:

```yaml
grocy-sso.yaml: |
  version: 1
  metadata:
    name: SSO - Grocy
  entries:
    - model: authentik_providers_proxy.proxyprovider
      id: grocy-provider
      state: present
      identifiers:
        name: grocy
      attrs:
        external_host: "https://grocy.allegedly.works"
        internal_host: "http://grocy.grocy.svc.cluster.local:80"
        mode: proxy
        authentication_flow: !Find [authentik_flows.flow, [slug, default-authentication-flow]]
        authorization_flow: !Find [authentik_flows.flow, [slug, default-provider-authorization-implicit-consent]]
        invalidation_flow: !Find [authentik_flows.flow, [slug, default-provider-invalidation-flow]]
        access_token_validity: "hours=24"

    - model: authentik_core.application
      id: grocy-app
      state: present
      identifiers:
        slug: grocy
      attrs:
        name: Grocy
        protocol_provider: !KeyOf grocy-provider
        meta_description: "Groceries & household management"
        meta_launch_url: "https://grocy.allegedly.works"
        open_in_new_tab: true

    - model: authentik_policies.policybinding
      state: present
      identifiers:
        target: !KeyOf grocy-app
        group: !Find [authentik_core.group, [name, authentik Admins]]
        order: 0

    - model: authentik_outposts.outpost
      state: present
      identifiers:
        name: grocy-outpost
      attrs:
        type: proxy
        protocol_providers:
          - !KeyOf grocy-provider
        service_connection: !Find [authentik_outposts.kubernetesserviceconnection, [name, Local Kubernetes Cluster]]
        config:
          authentik_host: "http://authentik-server.authentik:80"
          authentik_host_browser: "https://auth.allegedly.works"
          kubernetes_json_patches:
            deployment:
              - op: add
                path: /spec/template/spec/nodeSelector
                value:
                  topology.kubernetes.io/region: hetzner
```

### `k8s/authentik-proxy-routes/kustomization.yaml`

Add `grocy-httproute.yaml` to the `resources` list.

### `k8s/kustomization.yaml`

Add two lines:

- `- grocy-namespace` in the namespaces section
- `- applications/grocy` in the applications section

---

## No Terraform changes needed

Proxy-mode outposts require no OAuth2 client secret. No new entry in
`terraform/gitops/sso-secrets/` (compare: Gitea needs `sso/gitea` in Vault because it handles
OIDC client exchange itself).

---

## Dependency graph

```text
grocy-namespace
       ↓
 applications/grocy  ←  depends on: gateway, proxmox-csi,
                                     cert-manager-issuer-config,
                                     cert-manager-environment,
                                     authentik

authentik-proxy-routes  ←  grocy-httproute.yaml added
                            (existing Flux Kustomization, no dep changes)
```

Authentik auto-creates the `ak-outpost-grocy-outpost` Deployment and Service in the
`authentik` namespace when the blueprint applies (within ~60 s of sync).

---

## TODO: Grocy API token for agent access

After Grocy is running, provision an API token so OpenClaw and Claude can interact with the
Grocy REST API programmatically:

1. Log in to `https://grocy.allegedly.works` and navigate to **Manage API keys** in the user
   menu (top-right).
2. Create a new API key for agent use (e.g., name it `openclaw` or `claude-agent`).
3. Store the token in Vault at `kv/grocy/api-key` (property `api_key`).
4. Create an ExternalSecret to expose it to workloads that need it — e.g., mount into the
   OpenClaw pod as `GROCY_API_KEY`, or retrieve from Vault directly via the Vault API.

The Grocy REST API is available at `https://grocy.allegedly.works/api/`.

---

## Traffic flow (post-deploy)

```text
User browser
    → Gateway API HTTPS listener *.allegedly.works:443
    → HTTPRoute grocy.allegedly.works (in authentik-proxy-routes)
    → ak-outpost-grocy-outpost:9000  (Authentik proxy outpost, authentik ns)
    → [Authentik auth check — redirect to auth.allegedly.works if not logged in]
    → http://grocy.grocy.svc.cluster.local:80  (Grocy app)
```
