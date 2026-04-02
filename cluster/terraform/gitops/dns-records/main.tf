# Route 53 glue records and domain registration.
#
# PowerDNS zone records (NS, nebula lighthouse, API endpoint) are managed
# declaratively as ClusterRRset CRDs in k8s/powerdns/zones/dns-records.yaml
# with IPs substituted by Flux from the cluster-info ConfigMap.
#
# This module handles only the AWS side: glue records at the registrar level
# and nameserver delegation, which require the AWS API (not PowerDNS).

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

# Read VPS IPs from ConfigMap created by infrastructure terraform
data "kubernetes_config_map" "cluster_info" {
  metadata {
    name      = "cluster-info"
    namespace = "kube-system"
  }
}

locals {
  vps_cp_nodes = jsondecode(data.kubernetes_config_map.cluster_info.data["vps_cp_nodes"])
  domain       = "allegedly.works"

  ns_records = {
    for k, v in local.vps_cp_nodes : k => {
      ns_name = "ns${tonumber(substr(k, 3, -1)) + 1}" # vps0 -> ns1, vps1 -> ns2
      ip      = v.ip
    }
  }
}

# AWS provider configured via environment variables from secret
provider "aws" {
  region = var.aws_region
  # AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from environment
}

# Route 53 glue records (at registrar level)
# allow_overwrite: records persist in AWS across cluster destroy/recreate cycles
# (tofu-controller state is lost but Route 53 records remain)
resource "aws_route53_record" "ns_glue" {
  for_each = local.ns_records
  #checkov:skip=CKV2_AWS_23:Glue records point to external Hetzner VPS servers, not AWS resources

  zone_id         = var.route53_zone_id
  name            = "${each.value.ns_name}.${local.domain}"
  type            = "A"
  ttl             = 300
  records         = [each.value.ip]
  allow_overwrite = true
}

# Import: domain registration persists across cluster lifecycles.
# Declarative import is idempotent — no-op when already in state.
import {
  to = aws_route53domains_registered_domain.allegedly_works
  id = "allegedly.works"
}

# Update registered domain nameservers to point to our PowerDNS
# This changes the delegation at the TLD level from AWS Route 53 to our servers
resource "aws_route53domains_registered_domain" "allegedly_works" {
  domain_name = local.domain

  dynamic "name_server" {
    for_each = local.ns_records
    content {
      name     = "${name_server.value.ns_name}.${local.domain}"
      glue_ips = [name_server.value.ip]
    }
  }

  # Only manage nameservers. Ignoring other attributes avoids IAM permissions
  # for transfer lock, auto-renew, contacts, and privacy management.
  lifecycle {
    ignore_changes = [
      transfer_lock, auto_renew,
      admin_contact, billing_contact, registrant_contact, tech_contact,
      admin_privacy, billing_privacy, registrant_privacy, tech_privacy,
    ]
  }
}
