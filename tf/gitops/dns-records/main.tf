# Route 53 domain delegation for allegedly.works; DNS records are ExternalDNS-owned.
#
# All DNS is served by AWS Route 53. No in-cluster DNS authority.

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  domain = "allegedly.works"
}

provider "aws" {
  region = var.aws_region
}

# Read hosted zone to get its NS records for domain delegation
data "aws_route53_zone" "zone" {
  zone_id = var.route53_zone_id
}

# ExternalDNS owns these seven record sets and their TXT registry markers after
# the previous adoption stage. Forget every address without deleting Route 53
# records. CLEANUP(added 2026-10-05): Remove these blocks only after the
# dns-records state has applied the handoff. Domain registration stays in Terraform.
removed {
  from = aws_route53_record.wildcard
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.apex
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.mx_host
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.apex_mx
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.apex_spf
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.dmarc
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route53_record.api
  lifecycle {
    destroy = false
  }
}

# A resource address covers all seven for_each marker instances.
removed {
  from = aws_route53_record.external_dns_ownership
  lifecycle {
    destroy = false
  }
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
