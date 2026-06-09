# DNS

DNS for `allegedly.works` is served by AWS Route 53. Records are managed by
Terraform via tofu-controller.

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

OVH gateway node IPs are hardcoded in the Terraform module. Update when adding
or removing gateway nodes.

## Key Files

| File                                                     | Purpose                              |
| -------------------------------------------------------- | ------------------------------------ |
| `tf/gitops/dns-records/main.tf`                          | Route 53 records + domain delegation |
| `k8s/dns-automation/dns-records-tf.yaml`                 | tofu-controller Terraform resource   |
| `k8s/dns-automation/aws-credentials.sops.yaml`           | AWS IAM credentials (SOPS)           |
| `k8s/cert-manager/config/base/aws-credentials.sops.yaml` | AWS creds for cert-manager (SOPS)    |

### IAM User: `cluster-dns-manager`

Dedicated user with Route 53 policy. Credentials in SOPS-encrypted secrets
(see table above). IAM policy documented in <docs/iam-policy-route53.json>.

## Verification

```bash
# Check DNS resolution
dig allegedly.works A +short
dig api.allegedly.works A +short

# Check Route 53 nameservers
dig allegedly.works NS

# Check certificate status
kubectl get certificate -A
```

## Updating Gateway Node IPs

When adding or removing gateway nodes, update the IP locals in
`tf/gitops/dns-records/main.tf`. Commit and push; tofu-controller applies
automatically.
