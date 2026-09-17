# Browsertrix private web archive trial

This is an experimental Browsertrix deployment. It captures websites with real
browsers and stores portable WACZ files in the retained SeaweedFS `browsertrix`
bucket. The application is available at `https://archive.allegedly.works` only
through the shared Authentik proxy outpost; registration and public sharing are
disabled by default. Browsertrix retains its local superuser for application
administration, but Authentik is the external access boundary.

The upstream Browsertrix chart is vendored at `cluster/charts/browsertrix` and
records its source plus downstream changes in `UPSTREAM.md`. In particular, the
upstream chart's `system:anonymous` RoleBinding is removed. Browsertrix's backend
and generated background jobs authenticate to Kubernetes with the dedicated
`browsertrix-controller` ServiceAccount, while unrelated workloads do not mount
that token.

The cluster's kube-apiserver also rejects unauthenticated requests (`401`) and
Talos renders `--anonymous-auth=false`; removing the RoleBinding remains
important defense in depth if cluster authentication settings ever change.

Crawlers run in the chart-managed `browsertrix-crawlers` namespace with an
egress NetworkPolicy that blocks private, link-local and metadata ranges while
allowing DNS, per-crawl Redis, the Browsertrix frontend for QA, and the dedicated
SeaweedFS S3 service. WACZ downloads are proxied through `/data/` on the
Browsertrix frontend, keeping the bucket off the public S3 gateway and avoiding
browser CORS configuration.
