# DNS

DNS for `allegedly.works` is served by AWS Route 53. ExternalDNS manages public
application and static records; cert-manager manages ACME challenge TXT records.
Terraform still owns domain registration and Route 53 nameserver delegation,
with a two-hour drift-check interval.

ExternalDNS runs in the `external-dns` namespace. It reads accepted HTTPRoutes attached to
`gateway-system/cluster-gateway` and the `external-dns/static-records`
`DNSEndpoint`. It is limited to the
`allegedly.works` hosted zone, uses the TXT registry owner
`ducktape-allegedly-works`, and runs with `upsert-only` policy. The static endpoint's IP
targets come from the `nebula-mesh.json` roster. The
TXT registry replaces `*` with `wildcard` in its ownership marker name.
Accepted HTTPRoute hostnames get explicit A records pointing at the Gateway's
public target IPs. These explicit names take precedence over the wildcard.
With `upsert-only`, deleting an HTTPRoute does not remove its DNS record;
record deletion needs a separately reviewed policy change or cleanup.

ExternalDNS uses the dedicated IAM user `cluster-external-dns` with record
permissions limited to this hosted zone. Its key is stored in a separate SOPS
Secret and is not shared with Terraform's registrar-capable credential. The
original record and marker addresses were removed from Terraform state with
`removed` blocks and `destroy = false`; the registered domain stays in Terraform.

The seven existing record sets have ExternalDNS TXT registry markers. These use
`heritage=external-dns,external-dns/owner=ducktape-allegedly-works`; ExternalDNS
recognizes the owner without a resource label. The marker names use the configured
`external-dns-%{record_type}.` prefix and `wildcard` replacement.

## Architecture

```text
AWS Route 53 hosted zone (Z02901943N8ZFQFOD9P5I)
├── HTTPRoute hostnames       A    → Gateway public target IPs (ExternalDNS)
├── wildcard, mx, api        A    → roster public/control-plane IPs (ExternalDNS)
├── allegedly.works          MX   → mx.allegedly.works (ExternalDNS)
├── allegedly.works, _dmarc  TXT  → SPF/DMARC (ExternalDNS)
└── _acme-challenge.*        TXT  → ACME DNS-01 (cert-manager)
```

The Gateway annotation and the static gateway and API IPs in
`generated/external-dns-records/` are rendered from `nebula-mesh.json`
(<mesh_membership.md>). The apex A record comes from its accepted HTTPRoute and
uses the Gateway's target annotation. Static records use a 300-second TTL except
for the API A record, which uses 60 seconds. Route 53 requires literal double quotes
around TXT values in the `DNSEndpoint` targets, including SPF and DMARC.

## Key Files

| File                                                      | Purpose                                                                   |
| --------------------------------------------------------- | ------------------------------------------------------------------------- |
| `tf/gitops/dns-records/main.tf`                           | Domain registration, delegation, and record state handoff                 |
| `generated/dns-automation/dns-automation.k8s.yaml`        | tofu-controller Terraform resource (generated, `cdk8s/dns_automation.py`) |
| `k8s/external-creds/aws-route53-dns-automation.sops.yaml` | Canonical AWS IAM Secret for DNS automation (SOPS)                        |
| `k8s/external-creds/aws-route53-external-dns.sops.yaml`   | Dedicated ExternalDNS IAM key (SOPS)                                      |
| `k8s/external-creds/aws-route53-cert-manager.sops.yaml`   | Canonical AWS IAM Secret for cert-manager (SOPS)                          |
| `cdk8s/dns_automation.py`                                 | ESO destination Secret for Terraform in `flux-system`                     |
| `cdk8s/external_dns.py`                                   | ExternalDNS HelmRelease and ESO credential copy                           |
| `generated/external-dns-records/`                         | DNSEndpoint for wildcard, API, and mail records                           |
| `cdk8s/cert_manager/environment.py`                       | ESO destination Secret for cert-manager                                   |

### IAM User: `cluster-dns-manager`

Dedicated user with Route 53 and registrar permissions for Terraform.
Credentials are in SOPS-encrypted secrets (see table above). IAM policy:
<iam-policy-route53.json>.

### IAM User: `cluster-external-dns`

Dedicated user for ExternalDNS. Its inline policy is documented in
<iam-policy-external-dns.json>. The user and policy are provisioned in AWS IAM;
the access key is stored in the ExternalDNS SOPS Secret above. The policy permits
record changes only in hosted zone `Z02901943N8ZFQFOD9P5I` and has no Route 53
Domains permissions.

## Verification

```bash
# Check DNS resolution
dig allegedly.works A +short
dig api.allegedly.works A +short

# Check Route 53 nameservers
dig allegedly.works NS

# Check certificate status
kubectl get certificate -A

# Inspect ExternalDNS changes after its release is Ready
kubectl -n external-dns logs deployment/external-dns --since=15m
```

## Updating Gateway Node IPs

Edit the node's `nebula-mesh.json` entry and
`bb run //cluster/cdk8s:generate_manifests`, which rewrites the static
`DNSEndpoint` targets. Commit and push; ExternalDNS applies the new IPs.
