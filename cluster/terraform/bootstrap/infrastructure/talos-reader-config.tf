# Talos readonly client configuration (os:reader role)
#
# The talos_client_configuration data source always generates os:admin certs.
# To produce an os:reader cert we sign our own client certificate with the
# Talos OS CA, setting Organization = "os:reader" (how Talos encodes RBAC roles
# in X.509 — see https://docs.siderolabs.com/talos/v1.9/security/rbac).

resource "tls_private_key" "talos_reader" {
  algorithm = "ED25519"
}

resource "tls_cert_request" "talos_reader" {
  private_key_pem = tls_private_key.talos_reader.private_key_pem

  subject {
    organization = "os:reader"
  }
}

resource "tls_locally_signed_cert" "talos_reader" {
  cert_request_pem   = tls_cert_request.talos_reader.cert_request_pem
  ca_private_key_pem = base64decode(talos_machine_secrets.cluster.machine_secrets.certs.os.key)
  ca_cert_pem        = base64decode(talos_machine_secrets.cluster.machine_secrets.certs.os.cert)

  validity_period_hours = 8760 # 1 year

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "client_auth",
  ]
}

locals {
  talos_reader_config = yamlencode({
    context = var.cluster_name
    contexts = {
      (var.cluster_name) = {
        endpoints = local.all_controlplane_ips
        ca        = talos_machine_secrets.cluster.machine_secrets.certs.os.cert
        crt       = base64encode(tls_locally_signed_cert.talos_reader.cert_pem)
        key       = base64encode(tls_private_key.talos_reader.private_key_pem)
      }
    }
  })
}

resource "local_file" "talosconfig_reader" {
  content  = local.talos_reader_config
  filename = "${path.module}/talosconfig-reader.yml"
}

# TODO: Deploy the readonly Talos credentials into the cluster as a K8s Secret,
# readable by openclaw-sandbox and claude-sandbox namespaces. Pattern: store the
# reader config in Vault (via Terraform output → Vault KV), then use ExternalSecrets
# in claude-sandbox-secrets/ and openclaw-sandbox-secrets/ to read it into each
# namespace (following the existing Vault → ESO pattern used for other shared secrets).
