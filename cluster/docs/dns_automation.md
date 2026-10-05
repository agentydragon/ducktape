# DNS

DNS for `allegedly.works` is served by AWS Route 53. Records are managed by
Terraform via tofu-controller.

ExternalDNS is installed in the `external-dns` namespace in dry-run mode as the
first migration stage. It reads accepted HTTPRoutes attached to
`gateway-system/cluster-gateway` and the `external-dns/static-records`
`DNSEndpoint`, but its
`--dry-run` flag prevents Route 53 changes. It is limited to the
`allegedly.works` hosted zone, uses the TXT registry owner
`ducktape-allegedly-works`, and runs with `upsert-only` policy. Terraform remains
the record owner until a separately reviewed handoff. The static endpoint's IP
targets come from the same `nebula-mesh.json` roster as Terraform's inputs. The
TXT registry replaces `*` with `wildcard` in its ownership marker name.

ExternalDNS uses the dedicated IAM user `cluster-external-dns` with record
permissions limited to this hosted zone. Its key is stored in a separate SOPS
Secret and is not shared with Terraform's registrar-capable credential. Before
enabling writes, remove the record and marker addresses from Terraform state
without destroying the Route 53 records; keep the registered domain resource
in Terraform.

The seven existing record sets have Terraform-owned ExternalDNS TXT registry
markers while the controller remains dry-run. These markers use
`heritage=external-dns,external-dns/owner=ducktape-allegedly-works`; ExternalDNS
recognizes the owner without a resource label. The marker names use the configured
`external-dns-%{record_type}.` prefix and `wildcard` replacement. The next
handoff must forget the seven record addresses **and** the seven marker addresses
with `removed` blocks and `destroy = false`, then verify the Route 53 record
sets remain intact before enabling ExternalDNS writes.

## Architecture

```text
AWS Route 53 hosted zone (Z02901943N8ZFQFOD9P5I)
├── *.allegedly.works  A  → OVH gateway node IPs (wildcard)
├── allegedly.works    A  → OVH gateway node IPs (apex)
└── _acme-challenge.*  TXT  (managed by cert-manager for ACME DNS-01)

Terraform (tofu-controller) manages A records.
cert-manager Route 53 solver manages ACME challenge TXT records.
```

## Records

| Record   | FQDN                 | IPs                  | TTL |
| -------- | -------------------- | -------------------- | --- |
| wildcard | `*.allegedly.works.` | OVH gateway node IPs | 300 |
| apex     | `allegedly.works.`   | OVH gateway node IPs | 300 |

The gateway and API node IPs are the `public_nodes` var on the generated Terraform CR
(`generated/dns-automation/dns-automation.k8s.yaml`), rendered from `nebula-mesh.json`
(<mesh_membership.md>).

## Key Files

| File                                                      | Purpose                                                                   |
| --------------------------------------------------------- | ------------------------------------------------------------------------- |
| `tf/gitops/dns-records/main.tf`                           | Route 53 records + domain delegation                                      |
| `generated/dns-automation/dns-automation.k8s.yaml`        | tofu-controller Terraform resource (generated, `cdk8s/dns_automation.py`) |
| `k8s/external-creds/aws-route53-dns-automation.sops.yaml` | Canonical AWS IAM Secret for DNS automation (SOPS)                        |
| `k8s/external-creds/aws-route53-external-dns.sops.yaml`   | Dedicated ExternalDNS IAM key (SOPS)                                      |
| `k8s/external-creds/aws-route53-cert-manager.sops.yaml`   | Canonical AWS IAM Secret for cert-manager (SOPS)                          |
| `cdk8s/dns_automation.py`                                 | ESO destination Secret for Terraform in `flux-system`                     |
| `cdk8s/external_dns.py`                                   | Dry-run ExternalDNS HelmRelease and ESO credential copy                   |
| `generated/external-dns-records/`                         | Dry-run DNSEndpoint for wildcard, API, and mail records                   |
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

# Inspect proposed changes after the ExternalDNS release is Ready
kubectl -n external-dns logs deployment/external-dns --since=15m
```

## Updating Gateway Node IPs

Edit the node's `nebula-mesh.json` entry and
`bb run //cluster/cdk8s:generate_manifests`, which rewrites the Terraform CR's
`public_nodes` var. Commit and push; tofu-controller applies automatically.
