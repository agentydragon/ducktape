# Route 53 DNS for allegedly.works — zone records and domain delegation.
#
# All DNS is served by AWS Route 53. No in-cluster DNS authority.
# Update public_gateway_ips when adding/removing public Gateway nodes.

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  domain = "allegedly.works"

  # Public Gateway node IPs. Update when public Gateway-capable nodes change.
  # Comments name the current node (post talos-* → ovh-ns* renames). An IP listed
  # here lands in the `*.allegedly.works` round-robin, so a dead IP gives every
  # consumer a ~1/N transient failure rate — only list IPs whose node is currently
  # announcing the Cilium gateway.
  public_gateway_ips = [
    "147.135.37.175", # ovh-ns102453 (formerly talos-kimsufi-cp-0)
    "147.135.39.162", # ovh-ns103656 (formerly talos-kimsufi-worker-0)
    "147.135.104.16", # ovh-ns104963 (formerly talos-ks-game-worker-1)
    # Dropped 2026-06-02: `.176` (ovh-ns103711, formerly talos-kimsufi-worker-1) and
    # `.5` (ovh-ns104952, formerly talos-ks-game-worker-0) — both nodes are up but
    # the IPs return RST (Cilium L2 announce not active on these post-rename), so
    # the augur oauth2-proxy and similar consumers intermittently fail OIDC
    # discovery when their DNS round-robin lands on one. Restore once L2
    # announcement / per-node public-IP wiring is fixed.
  ]

  # Kubernetes API endpoints — every control-plane node, not just the ones whose
  # Cilium gateway is healthy. The apiserver listens on the host directly so it
  # is independent of L2-announce state: `ovh-ns103711` (`.176`) is dropped from
  # `public_gateway_ips` above for gateway-RST reasons but still serves the API
  # on :6443.
  kube_api_ips = [
    "147.135.37.175", # ovh-ns102453 (formerly talos-kimsufi-cp-0)
    "147.135.39.162", # ovh-ns103656 (formerly talos-kimsufi-worker-0)
    "147.135.39.176", # ovh-ns103711 (formerly talos-kimsufi-worker-1)
  ]
}

provider "aws" {
  region = var.aws_region
}

# Read hosted zone to get its NS records for domain delegation
data "aws_route53_zone" "zone" {
  zone_id = var.route53_zone_id
}

import {
  to = aws_route53_record.wildcard
  id = "Z02901943N8ZFQFOD9P5I_*.allegedly.works_A"
}

import {
  to = aws_route53_record.apex
  id = "Z02901943N8ZFQFOD9P5I_allegedly.works_A"
}

# Wildcard A record — all subdomains resolve to VPS nodes
resource "aws_route53_record" "wildcard" {
  #checkov:skip=CKV2_AWS_23:A records point to external public gateway nodes, not AWS resources
  zone_id         = var.route53_zone_id
  name            = "*.${local.domain}"
  type            = "A"
  ttl             = 300
  records         = local.public_gateway_ips
  allow_overwrite = true
}

# Apex A record
resource "aws_route53_record" "apex" {
  #checkov:skip=CKV2_AWS_23:A records point to external public gateway nodes, not AWS resources
  zone_id         = var.route53_zone_id
  name            = local.domain
  type            = "A"
  ttl             = 300
  records         = local.public_gateway_ips
  allow_overwrite = true
}

# Kubernetes API A record. This intentionally overrides the wildcard record
# because kubeconfigs connect to api.allegedly.works:6443.
resource "aws_route53_record" "api" {
  #checkov:skip=CKV2_AWS_23:A records point to external Kubernetes API nodes, not AWS resources
  zone_id         = var.route53_zone_id
  name            = "api.${local.domain}"
  type            = "A"
  ttl             = 60
  records         = local.kube_api_ips
  allow_overwrite = true
}

# Domain registration — delegate to Route 53 nameservers
import {
  to = aws_route53domains_registered_domain.allegedly_works
  id = "allegedly.works"
}

resource "aws_route53domains_registered_domain" "allegedly_works" {
  domain_name = local.domain

  dynamic "name_server" {
    for_each = toset(data.aws_route53_zone.zone.name_servers)
    content {
      name = name_server.value
    }
  }

  lifecycle {
    ignore_changes = [
      transfer_lock, auto_renew,
      admin_contact, billing_contact, registrant_contact, tech_contact,
      admin_privacy, billing_privacy, registrant_privacy, tech_privacy,
    ]
  }
}
