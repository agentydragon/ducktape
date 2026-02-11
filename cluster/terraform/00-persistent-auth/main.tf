# LAYER 0: PERSISTENT AUTH
# Persistent authentication credentials that survive VM lifecycle
# Includes: CSI tokens, sealed secrets keypair, persistent auth storage

terraform {
  required_version = ">= 1.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1.0"
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.10.0"
    }
  }
}

# DRY configuration for persistent auth
locals {
  # NOTE: SSH uses proxmox_ssh_host (atlas) not proxmox_api_host (FQDN)
  # because the FQDN routes through VPS nginx, but SSH needs direct Tailscale access
  proxmox_ssh_target = "root@${var.proxmox_ssh_host}"

  # Persistent Proxmox users - survive VM lifecycle
  pve_persistent_users = {
    csi = {
      name    = "kubernetes-csi@pve"
      comment = "Kubernetes CSI driver service account (persistent)"
      role    = "CSI"
      privs   = "VM.Audit,VM.Config.Disk,Datastore.Allocate,Datastore.AllocateSpace,Datastore.Audit"
      token   = "csi"
    }
    terraform = {
      name    = "terraform@pve"
      comment = "Terraform automation user (persistent)"
      role    = "TerraformAdmin"
      privs   = "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Pool.Allocate,SDN.Use,Sys.Audit,Sys.Console,Sys.Modify,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.Monitor,VM.PowerMgmt,User.Modify,Permissions.Modify"
      token   = "terraform-token"
    }
  }
}

# Auto-provision persistent Proxmox users and tokens via SSH
data "external" "pve_persistent_tokens" {
  for_each = local.pve_persistent_users

  program = ["bash", "-c", <<-EOT
    token_json=$(ssh ${local.proxmox_ssh_target} '
      # Create user if not exists
      pveum user add ${each.value.name} --comment "${each.value.comment}" 2>/dev/null || true

      # Create role if not exists
      pveum role add ${each.value.role} -privs "${each.value.privs}" 2>/dev/null || true

      # Set ACL permissions
      pveum aclmod / -user ${each.value.name} -role ${each.value.role}

      # Create/recreate API token with JSON output
      pveum user token delete ${each.value.name} ${each.value.token} 2>/dev/null || true
      pveum user token add ${each.value.name} ${each.value.token} --privsep 0 --output-format json
    ')
    # Extract the token value and create complete CSI configuration
    token_value=$(echo "$token_json" | jq -r '.value')
    token_id="${each.value.name}!${each.value.token}"

    # Create CSI config JSON and properly escape it as a string
    csi_config_json=$(cat <<JSON
{"url":"https://${var.proxmox_api_host}:8006/api2/json","insecure":true,"token_id":"$token_id","token_secret":"$token_value","region":"proxmox","token":"$token_id=$token_value"}
JSON
)
    # Output for terraform external - wrap JSON as escaped string
    printf '{"config_json":"%s"}' "$(echo "$csi_config_json" | sed 's/"/\\"/g')"
  EOT
  ]
}

# Sealed secrets keypair now managed in secrets.tf using tls_private_key
# No libsecret dependency - keys persist in terraform state

# Generate Proxmox CSI storage secrets using terraform-generated sealed-secrets keypair
resource "null_resource" "proxmox_csi_sealed_secret" {
  # Re-run when PVE auth tokens or keypair change
  triggers = {
    csi_config_hash = sha256(jsonencode(data.external.pve_persistent_tokens["csi"].result.config_json))
    keypair_hash    = sha256(tls_self_signed_cert.sealed_secrets.cert_pem)
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      # Create temporary secret file with CSI configuration
      csi_config='${data.external.pve_persistent_tokens["csi"].result.config_json}'

      # Create kubernetes secret YAML
      cat > /tmp/proxmox-csi-secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: proxmox-csi-plugin
  namespace: csi-proxmox
type: Opaque
stringData:
  config.yaml: |
    clusters:
      - url: $(echo "$csi_config" | jq -r .url)
        insecure: $(echo "$csi_config" | jq -r .insecure)
        token_id: $(echo "$csi_config" | jq -r .token_id)
        token_secret: $(echo "$csi_config" | jq -r .token_secret)
        region: $(echo "$csi_config" | jq -r .region)
EOF

      # Seal the secret using terraform-generated keypair
      cat > /tmp/sealed-secrets-cert.pem <<'CERTEOF'
${tls_self_signed_cert.sealed_secrets.cert_pem}
CERTEOF
      kubeseal --cert /tmp/sealed-secrets-cert.pem \
        --format=yaml < /tmp/proxmox-csi-secret.yaml > ./../../k8s/storage/proxmox-csi-sealed.yaml
      rm /tmp/sealed-secrets-cert.pem

      # Clean up temporary file
      rm /tmp/proxmox-csi-secret.yaml

      echo "✅ Generated sealed secret for Proxmox CSI"
    EOT
  }
}

# NOTE: Auto-commit removed - user must manually commit sealed secrets after terraform apply
# Run: git add k8s/storage/proxmox-csi-sealed.yaml && git commit -m "chore: update sealed secret"
#
# The seal-secret.sh helper script reads the cert directly from terraform state
# via `terraform output -raw sealed_secrets_cert_pem`

# NOTE: No cleanup provisioner here - persistent tokens only destroyed when this layer is explicitly destroyed
