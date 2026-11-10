# Browsertrix private web archive trial

This is the retained revival package for a decommissioned Browsertrix trial.
It is not an active deployment. The trial captured websites with real browsers
and stored portable WACZ files in the retained SeaweedFS `browsertrix` bucket.
Its endpoint was `https://archive.allegedly.works`, behind the shared Authentik
proxy outpost. Registration and public sharing were disabled by default.
Browsertrix had a local superuser for application administration, while
Authentik provided the external access boundary.

The upstream Browsertrix chart is vendored at `cluster/charts/browsertrix` and
records its source plus downstream changes in `UPSTREAM.md`. In particular, the
upstream chart's `system:anonymous` RoleBinding is removed. In the retained
configuration, Browsertrix's backend and generated background jobs use the
dedicated `browsertrix-controller` ServiceAccount, while unrelated workloads do
not mount that token.

When the trial was deployed, the cluster's kube-apiserver rejected
unauthenticated requests (`401`) and Talos rendered `--anonymous-auth=false`.
The removed RoleBinding remains part of the revival package's defense in depth.

The retained chart places crawlers in its `browsertrix-crawlers` namespace. Its
egress NetworkPolicy blocks private, link-local and metadata ranges while
allowing DNS, per-crawl Redis, the Browsertrix frontend for QA, and the dedicated
SeaweedFS S3 service. The frontend proxies WACZ downloads through `/data/`,
keeping the bucket off the public S3 gateway and avoiding browser CORS
configuration if the trial is revived.
