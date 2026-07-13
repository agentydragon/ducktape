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

  # Public Gateway node IPs. This should match every public OVH Kubernetes node
  # in nebula-mesh.json with role=control-plane or role=worker. The validation
  # test in cluster/validation/test_dns_records.py fails if this hand-written
  # Terraform list drifts from that roster.
  public_gateway_ips = [
    "147.135.37.175", # ovh-ns102453 (formerly talos-kimsufi-cp-0)
    "147.135.39.162", # ovh-ns103656 (formerly talos-kimsufi-worker-0)
    "147.135.39.176", # ovh-ns103711 (formerly talos-kimsufi-worker-1)
    "147.135.104.5",  # ovh-ns104952 (formerly talos-ks-game-worker-0)
    "147.135.104.16", # ovh-ns104963 (formerly talos-ks-game-worker-1)
  ]

  # Kubernetes API endpoints — every control-plane node. The apiserver listens on
  # the host directly on :6443, independent of Cilium gateway/L2-announce state.
  # Post Stage-2 etcd reshuffle (cluster/docs/plans/ovh_storage_tiering.md) the
  # control plane is 103656 (KS-5 anchor) + the two KS-GAME NVMe nodes; 102453 and
  # 103711 were demoted to workers and no longer serve the API.
  kube_api_ips = [
    "147.135.39.162", # ovh-ns103656
    "147.135.104.5",  # ovh-ns104952
    "147.135.104.16", # ovh-ns104963
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

# Wildcard A record — all subdomains resolve to public Kubernetes nodes.
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

# --- Inbound mail (the Haku mailbox, haku/mailbox/) ---------------------------
#
# mx.allegedly.works is already covered by the wildcard, but MX targets should
# not depend on wildcard semantics — keep an explicit A record on the same
# public-gateway roster. A haku-mailbox-smtp-ingress DaemonSet binds port 25 on
# every node and forwards the sending MTA's address to Stalwart with PROXY
# protocol (cluster/k8s/haku/mailbox/app/smtp-ingress.yaml).
resource "aws_route53_record" "mx_host" {
  #checkov:skip=CKV2_AWS_23:A records point to external public gateway nodes, not AWS resources
  zone_id         = var.route53_zone_id
  name            = "mx.${local.domain}"
  type            = "A"
  ttl             = 300
  records         = local.public_gateway_ips
  allow_overwrite = true
}

resource "aws_route53_record" "apex_mx" {
  zone_id         = var.route53_zone_id
  name            = local.domain
  type            = "MX"
  ttl             = 300
  records         = ["10 mx.${local.domain}"]
  allow_overwrite = true
}

# The domain receives mail but sends none: publish SPF -all + DMARC reject so
# nobody can spoof @allegedly.works senders. Revisit both if Haku ever gets an
# outbound-mail path on this domain.
resource "aws_route53_record" "apex_spf" {
  zone_id         = var.route53_zone_id
  name            = local.domain
  type            = "TXT"
  ttl             = 300
  records         = ["v=spf1 -all"]
  allow_overwrite = true
}

resource "aws_route53_record" "dmarc" {
  zone_id         = var.route53_zone_id
  name            = "_dmarc.${local.domain}"
  type            = "TXT"
  ttl             = 300
  records         = ["v=DMARC1; p=reject; adkim=s; aspf=s"]
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
