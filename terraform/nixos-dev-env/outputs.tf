# Outputs for NixOS Dev Environment

# Wyrm2 outputs
output "wyrm2" {
  description = "Wyrm2 VM info"
  value = {
    name           = module.wyrm2.vm_name
    id             = module.wyrm2.vm_id
    ipv4_addresses = module.wyrm2.ipv4_addresses
  }
}

output "instructions" {
  description = "Setup instructions and next steps"
  value       = <<-EOT

    ✅ Environment created successfully!

    VMs:
    - wyrm2 (ID: ${module.wyrm2.vm_id}) — dev workstation + k8s worker

    Next steps:

    1. Wait for VM to boot (~30 seconds)

    2. Get VM IP address:
       terraform output wyrm2

    3. SSH into the VM:
       ssh agentydragon@<vm-ip>

    4. Approve the kubelet CSR to join the cluster:
       kubectl get csr
       kubectl certificate approve <csr-name>

    5. Verify node joined:
       kubectl get nodes

    NOTE: tofu apply only provisions the VM and injects cloud-init
    credentials. It does NOT run nixos-rebuild — the VM boots with
    whatever NixOS config was baked into the qcow2 image. To apply
    NixOS config changes to a running VM, SSH in and run:

      sudo nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#wyrm2
  EOT
}
