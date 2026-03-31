# PERSISTENT AUTH
# Persistent authentication credentials that survive VM lifecycle.
# Includes: CSI tokens, sealed secrets keypair, Flux deploy key, Nebula PKI,
# Nix cache signing key, Attic JWT token, sealed secrets for k8s.

# ============================================================================
# PROXMOX USERS, ROLES, AND TOKENS
# ============================================================================

# TODO: Consider whether terraform@pve is still needed now that root@pam!tofu exists.
# Infrastructure and k8s-worker-proxmox could read the root token from the keyring via
# their own .envrc instead. Keeping for now for least-privilege (TerraformAdmin is
# narrower than root). kubernetes-csi@pve is definitely still needed — it runs inside
# the cluster and can't access the keyring.
locals {
  # Persistent Proxmox users - survive VM lifecycle
  pve_persistent_users = {
    csi = {
      name    = "kubernetes-csi@pve"
      comment = "Kubernetes CSI driver service account (persistent)"
      role    = "CSI"
      privs = [
        "Datastore.Allocate",
        "Datastore.AllocateSpace",
        "Datastore.Audit",
        "VM.Audit",
        "VM.Config.Disk",
      ]
      token = "csi"
    }
    terraform = {
      name    = "terraform@pve"
      comment = "Terraform automation user (persistent)"
      role    = "TerraformAdmin"
      privs = [
        "Datastore.Allocate",
        "Datastore.AllocateSpace",
        "Datastore.AllocateTemplate",
        "Datastore.Audit",
        "Mapping.Modify",
        "Mapping.Use",
        "Permissions.Modify",
        "Pool.Allocate",
        "SDN.Use",
        "Sys.Audit",
        "Sys.Console",
        "Sys.Modify",
        "User.Modify",
        "VM.Allocate",
        "VM.Audit",
        "VM.Clone",
        "VM.Config.CDROM",
        "VM.Config.CPU",
        "VM.Config.Cloudinit",
        "VM.Config.Disk",
        "VM.Config.HWType",
        "VM.Config.Memory",
        "VM.Config.Network",
        "VM.Config.Options",
        "VM.Console",
        "VM.GuestAgent.Audit",
        "VM.Migrate",
        "VM.PowerMgmt",
      ]
      token = "terraform-token"
    }
  }
}

resource "proxmox_virtual_environment_role" "persistent" {
  for_each   = local.pve_persistent_users
  role_id    = each.value.role
  privileges = each.value.privs

  lifecycle { prevent_destroy = true }
}

resource "proxmox_virtual_environment_user" "persistent" {
  for_each = local.pve_persistent_users
  user_id  = each.value.name
  comment  = each.value.comment
  acl {
    path      = "/"
    role_id   = proxmox_virtual_environment_role.persistent[each.key].role_id
    propagate = true
  }

  lifecycle { prevent_destroy = true }
}

resource "proxmox_virtual_environment_user_token" "persistent" {
  for_each              = local.pve_persistent_users
  user_id               = proxmox_virtual_environment_user.persistent[each.key].user_id
  token_name            = each.value.token
  privileges_separation = false
  comment               = "Managed by OpenTofu"

  lifecycle { prevent_destroy = true }
}

locals {
  # bpg/proxmox user_token.value returns "token_id=secret_uuid" (full API token string).
  # Split to extract just the UUID for consumers that need token_id and secret separately.
  pve_token_configs = {
    for key, user in local.pve_persistent_users : key => {
      url          = "https://${var.proxmox_api_host}:8006/api2/json"
      insecure     = true
      token_id     = proxmox_virtual_environment_user_token.persistent[key].id
      token_secret = element(split("=", proxmox_virtual_environment_user_token.persistent[key].value), 1)
      region       = "proxmox"
      token        = proxmox_virtual_environment_user_token.persistent[key].value
    }
  }
}

# ============================================================================
# SEALED SECRETS KEYPAIR (RSA 4096-bit, self-signed, 10-year validity)
# ============================================================================

resource "tls_private_key" "sealed_secrets" {
  algorithm = "RSA"
  rsa_bits  = 4096

  lifecycle { prevent_destroy = true }
}

resource "tls_self_signed_cert" "sealed_secrets" {
  private_key_pem = tls_private_key.sealed_secrets.private_key_pem

  subject {
    common_name  = "sealed-secret"
    organization = "sealed-secrets"
  }

  validity_period_hours = 87600 # 10 years
  is_ca_certificate     = true

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "cert_signing",
  ]

  lifecycle { prevent_destroy = true }
}

# Public certificate committed to repo (safe — only enables encrypting, not decrypting).
# seal-secret.sh reads this file instead of requiring terraform state access.
resource "local_file" "sealed_secrets_cert" {
  content  = tls_self_signed_cert.sealed_secrets.cert_pem
  filename = "${path.module}/../../k8s/sealed-secrets/sealed-secrets-cert.pem"
}

# ============================================================================
# FLUX DEPLOY KEY (ED25519 for GitHub repository access)
# ============================================================================

resource "tls_private_key" "flux_deploy" {
  algorithm = "ED25519"

  lifecycle { prevent_destroy = true }
}

# ============================================================================
# NIX CACHE SIGNING KEY
# Generated once via nix-store and cached in local file.
# File is gitignored, backed up with terraform state to Google Drive.
# ============================================================================

data "external" "nix_cache_key" {
  program = ["bash", "-c", <<-EOT
    KEY_FILE="${path.module}/nix-cache-key.json"
    if [ ! -f "$KEY_FILE" ]; then
      nix-store --generate-binary-cache-key cache.allegedly.works-1 /tmp/nix-priv.$$ /tmp/nix-pub.$$ 2>/dev/null
      jq -n --arg priv "$(cat /tmp/nix-priv.$$)" --arg pub "$(cat /tmp/nix-pub.$$)" \
        '{private_key: $priv, public_key: $pub}' > "$KEY_FILE"
      rm -f /tmp/nix-priv.$$ /tmp/nix-pub.$$
    fi
    cat "$KEY_FILE"
  EOT
  ]
}

locals {
  nix_cache_keys = data.external.nix_cache_key.result
}

# ============================================================================
# NEBULA MESH PKI — CA + per-node certificates
# ============================================================================

locals {
  nebula_cert_dir = "${path.module}/nebula-certs"

  # All Nebula mesh nodes — add new nodes here when expanding the mesh.
  nebula_nodes = {
    "talos-vps-cp-0"  = { ip = "10.42.0.1/16", groups = "lighthouse,controlplane,vps" }
    "talos-vps-cp-1"  = { ip = "10.42.0.2/16", groups = "lighthouse,controlplane,vps" }
    "talos-pve-cp-0"  = { ip = "10.42.0.10/16", groups = "controlplane,proxmox" }
    "wyrm2"           = { ip = "10.42.0.20/16", groups = "worker,proxmox,gpu" }
    "rugged"          = { ip = "10.42.0.30/16", groups = "worker,roaming" }
    "k8s-worker-test" = { ip = "10.42.0.99/16", groups = "worker,test" }
    "atlas"           = { ip = "10.42.0.5/16", groups = "hypervisor,proxmox" }
    "activitywatch"   = { ip = "10.42.0.40/16", groups = "service" }
  }
}

# Generate CA cert + key (once, stored on disk + in state).
resource "null_resource" "nebula_ca" {
  triggers = {
    cert_dir = local.nebula_cert_dir
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      mkdir -p ${local.nebula_cert_dir}
      if [ ! -f ${local.nebula_cert_dir}/ca.crt ]; then
        nebula-cert ca -name "allegedly.works" -duration 87600h \
          -out-crt ${local.nebula_cert_dir}/ca.crt \
          -out-key ${local.nebula_cert_dir}/ca.key
        echo "Generated new Nebula CA"
      else
        echo "Nebula CA already exists, skipping"
      fi
    EOT
  }
}

data "local_file" "nebula_ca_crt" {
  filename   = "${local.nebula_cert_dir}/ca.crt"
  depends_on = [null_resource.nebula_ca]
}

# Generate per-node certs signed by the CA.
resource "null_resource" "nebula_node_cert" {
  for_each = local.nebula_nodes

  triggers = {
    ca_hash = data.local_file.nebula_ca_crt.content_sha256
    ip      = each.value.ip
    groups  = each.value.groups
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      nebula-cert sign \
        -ca-crt ${local.nebula_cert_dir}/ca.crt \
        -ca-key ${local.nebula_cert_dir}/ca.key \
        -name "${each.key}" \
        -ip "${each.value.ip}" \
        -groups "${each.value.groups}" \
        -out-crt ${local.nebula_cert_dir}/${each.key}.crt \
        -out-key ${local.nebula_cert_dir}/${each.key}.key
    EOT
  }

  depends_on = [null_resource.nebula_ca]
}

data "local_file" "nebula_node_crt" {
  for_each   = local.nebula_nodes
  filename   = "${local.nebula_cert_dir}/${each.key}.crt"
  depends_on = [null_resource.nebula_node_cert]
}

data "local_sensitive_file" "nebula_node_key" {
  for_each   = local.nebula_nodes
  filename   = "${local.nebula_cert_dir}/${each.key}.key"
  depends_on = [null_resource.nebula_node_cert]
}

# ============================================================================
# PROXMOX CSI SEALED SECRET
# ============================================================================

resource "null_resource" "proxmox_csi_sealed_secret" {
  triggers = {
    csi_config_hash = sha256(jsonencode(local.pve_token_configs["csi"]))
    keypair_hash    = sha256(tls_self_signed_cert.sealed_secrets.cert_pem)
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e

      # Create kubernetes secret YAML with CSI configuration
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
      - url: ${local.pve_token_configs["csi"].url}
        insecure: ${local.pve_token_configs["csi"].insecure}
        token_id: ${local.pve_token_configs["csi"].token_id}
        token_secret: ${local.pve_token_configs["csi"].token_secret}
        region: ${local.pve_token_configs["csi"].region}
EOF

      # Seal the secret using terraform-generated keypair
      cat > /tmp/sealed-secrets-cert.pem <<'CERTEOF'
${tls_self_signed_cert.sealed_secrets.cert_pem}
CERTEOF
      kubeseal --cert /tmp/sealed-secrets-cert.pem \
        --format=yaml < /tmp/proxmox-csi-secret.yaml > ${path.module}/../../k8s/proxmox-csi/secrets/proxmox-csi-sealed.yaml
      rm /tmp/sealed-secrets-cert.pem

      # Clean up temporary file
      rm /tmp/proxmox-csi-secret.yaml

      echo "Generated sealed secret for Proxmox CSI"
    EOT
  }
}

# ============================================================================
# NIX CACHE SIGNING KEY SEALED SECRET
# ============================================================================

resource "null_resource" "nix_cache_signing_key_sealed_secret" {
  triggers = {
    keys_hash    = sha256("${local.nix_cache_keys.private_key}:${local.nix_cache_keys.public_key}")
    keypair_hash = sha256(tls_self_signed_cert.sealed_secrets.cert_pem)
  }

  provisioner "local-exec" {
    command = <<-EOT
      # Get keys from terraform-managed local file
      private_key='${local.nix_cache_keys.private_key}'
      public_key='${local.nix_cache_keys.public_key}'

      # Create kubernetes secret YAML
      cat > /tmp/nix-cache-signing-key.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: nix-cache-signing-key
  namespace: nix-cache
type: Opaque
stringData:
  signing-key.sec: |
    $private_key
  signing-key.pub: |
    $public_key
EOF

      # Seal the secret using terraform-generated keypair
      cat > /tmp/sealed-secrets-cert.pem <<'CERTEOF'
${tls_self_signed_cert.sealed_secrets.cert_pem}
CERTEOF
      kubeseal --cert /tmp/sealed-secrets-cert.pem \
        --format=yaml < /tmp/nix-cache-signing-key.yaml > ${path.module}/../../k8s/applications/nix-cache/signing-key-sealed.yaml
      rm /tmp/sealed-secrets-cert.pem

      # Clean up temporary file
      rm /tmp/nix-cache-signing-key.yaml

      echo "Generated sealed secret for Nix cache signing key"
    EOT
  }
}

# ============================================================================
# ATTIC JWT TOKEN
# ============================================================================

resource "random_password" "attic_jwt_token_raw" {
  length  = 48
  special = false
}

locals {
  attic_jwt_token_base64 = base64encode(random_password.attic_jwt_token_raw.result)
}

resource "null_resource" "attic_jwt_token_sealed_secret" {
  triggers = {
    token_hash   = sha256(local.attic_jwt_token_base64)
    keypair_hash = sha256(tls_self_signed_cert.sealed_secrets.cert_pem)
  }

  provisioner "local-exec" {
    command = <<-EOT
      # Get base64-encoded JWT token from terraform
      jwt_token_base64='${local.attic_jwt_token_base64}'

      # Create kubernetes secret YAML
      cat > /tmp/attic-jwt-token.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: attic-jwt-token
  namespace: nix-cache
type: Opaque
stringData:
  jwt-token: "$jwt_token_base64"
EOF

      # Seal the secret using terraform-generated keypair
      cat > /tmp/sealed-secrets-cert.pem <<'CERTEOF'
${tls_self_signed_cert.sealed_secrets.cert_pem}
CERTEOF
      kubeseal --cert /tmp/sealed-secrets-cert.pem \
        --format=yaml < /tmp/attic-jwt-token.yaml > ${path.module}/../../k8s/applications/nix-cache/jwt-token-sealed.yaml
      rm /tmp/sealed-secrets-cert.pem

      # Clean up temporary file
      rm /tmp/attic-jwt-token.yaml

      echo "Generated sealed secret for Attic JWT token (base64-encoded)"
    EOT
  }
}

# ============================================================================
# NEBULA ACTIVITYWATCH SEALED SECRET
# ============================================================================

resource "null_resource" "nebula_activitywatch_sealed_secret" {
  triggers = {
    ca_hash      = sha256(data.local_file.nebula_ca_crt.content)
    cert_hash    = sha256(data.local_file.nebula_node_crt["activitywatch"].content)
    key_hash     = sha256(data.local_sensitive_file.nebula_node_key["activitywatch"].content)
    keypair_hash = sha256(tls_self_signed_cert.sealed_secrets.cert_pem)
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e

      ca_crt='${data.local_file.nebula_ca_crt.content}'
      host_crt='${data.local_file.nebula_node_crt["activitywatch"].content}'
      host_key='${data.local_sensitive_file.nebula_node_key["activitywatch"].content}'

      cat > /tmp/nebula-activitywatch-secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: activitywatch-nebula-certs
  namespace: activitywatch
type: Opaque
stringData:
  ca.crt: |
$(echo "$ca_crt" | sed 's/^/    /')
  host.crt: |
$(echo "$host_crt" | sed 's/^/    /')
  host.key: |
$(echo "$host_key" | sed 's/^/    /')
EOF

      cat > /tmp/sealed-secrets-cert.pem <<'CERTEOF'
${tls_self_signed_cert.sealed_secrets.cert_pem}
CERTEOF

      kubeseal --cert /tmp/sealed-secrets-cert.pem \
        --format=yaml < /tmp/nebula-activitywatch-secret.yaml \
        > ${path.module}/../../k8s/applications/activitywatch/nebula-certs-sealed.yaml

      rm -f /tmp/sealed-secrets-cert.pem /tmp/nebula-activitywatch-secret.yaml

      echo "Generated sealed secret for ActivityWatch Nebula certs"
    EOT
  }

  depends_on = [
    null_resource.nebula_node_cert,
    null_resource.nebula_ca,
  ]
}
