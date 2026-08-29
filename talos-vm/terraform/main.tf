# Provider configuration
provider "talos" {}

# Working directory for VM files
locals {
  vm_dir = abspath("${path.root}/..")
  talos_version_short = replace(var.talos_version, "v", "")
}

# Generate Talos machine secrets
resource "talos_machine_secrets" "this" {
  talos_version = var.talos_version
}

# Generate Talos client configuration
data "talos_client_configuration" "this" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.this.client_configuration
  endpoints            = ["127.0.0.1"]
  nodes                = ["127.0.0.1"]
}

# Create custom Image Factory schematic (vanilla Talos for now)
resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = []
      }
    }
  })
}

# Get Image Factory URLs for downloading Talos images
data "talos_image_factory_urls" "this" {
  talos_version = var.talos_version
  schematic_id  = talos_image_factory_schematic.this.id
  platform      = "metal"
  architecture  = "amd64"
}

# Generate control plane machine configuration
data "talos_machine_configuration" "controlplane" {
  cluster_name       = var.cluster_name
  cluster_endpoint   = "https://127.0.0.1:6443"
  machine_type       = "controlplane"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version

  config_patches = [
    yamlencode({
      machine = {
        certSANs = ["127.0.0.1"]

        time = {
          disabled = true # NTP blocked, rely on QEMU RTC sync
        }

        env = {
          HTTP_PROXY  = var.proxy_url
          HTTPS_PROXY = var.proxy_url
          NO_PROXY    = var.no_proxy
        }

        network = {
          nameservers = var.dns_servers
        }

        install = {
          disk  = "/dev/vda" # virtio disk
          image = "ghcr.io/siderolabs/installer:${var.talos_version}"
        }

        registries = {
          config = {
            for registry in var.insecure_registries : registry => {
              tls = {
                insecureSkipVerify = true
              }
            }
          }
        }

        kubelet = {
          image = "ghcr.io/siderolabs/kubelet:${var.kubernetes_version}"
        }
      }

      cluster = {
        network = {
          dnsDomain      = "cluster.local"
          podSubnets     = ["10.244.0.0/16"]
          serviceSubnets = ["10.96.0.0/12"]
        }
      }
    })
  ]
}

# Download Talos kernel and initramfs
# NOTE: Using pxe.factory.talos.dev instead of factory.talos.dev (which redirects
# to assets.factory.talos.dev that is blocked by the sandbox proxy with 503 errors)
# and GitHub releases for initramfs as a reliable alternative.
resource "shell_script" "download_talos_images" {
  lifecycle_commands {
    create = <<-EOT
      mkdir -p ${local.vm_dir}/_out
      wget -O ${local.vm_dir}/_out/vmlinuz-amd64 "https://pxe.factory.talos.dev/image/${talos_image_factory_schematic.this.id}/${var.talos_version}/kernel-amd64"
      curl -L "https://github.com/siderolabs/talos/releases/download/${var.talos_version}/initramfs-amd64.xz" -o ${local.vm_dir}/_out/initramfs-amd64.xz
      echo "downloaded"
    EOT
    read   = "test -f ${local.vm_dir}/_out/vmlinuz-amd64 && test -f ${local.vm_dir}/_out/initramfs-amd64.xz && echo 'downloaded' || echo ''"
    delete = "rm -rf ${local.vm_dir}/_out || true"
  }
}

# Create disk image
resource "shell_script" "create_disk" {
  lifecycle_commands {
    create = "qemu-img create -f qcow2 ${local.vm_dir}/talos-disk-tf.qcow2 ${var.vm_disk_size}"
    delete = "rm -f ${local.vm_dir}/talos-disk-tf.qcow2 || true"
  }

  environment = {
    DISK_PATH = "${local.vm_dir}/talos-disk-tf.qcow2"
  }
}

# Start VM
resource "shell_script" "start_vm" {
  depends_on = [shell_script.create_disk, shell_script.download_talos_images]

  lifecycle_commands {
    create = <<-EOT
      nohup qemu-system-x86_64 \
        -name ${var.cluster_name} \
        -machine type=q35 \
        -cpu Nehalem \
        -m ${var.vm_memory} \
        -smp ${var.vm_cpus} \
        -drive file=${local.vm_dir}/talos-disk-tf.qcow2,if=virtio,format=qcow2 \
        -kernel ${local.vm_dir}/_out/vmlinuz-amd64 \
        -initrd ${local.vm_dir}/_out/initramfs-amd64.xz \
        -append "console=ttyS0 talos.platform=metal slab_nomerge pti=on" \
        -netdev user,id=net0,hostfwd=tcp::50000-:50000,hostfwd=tcp::6443-:6443,dns=8.8.8.8 \
        -device virtio-net-pci,netdev=net0 \
        -rtc base=utc,clock=host \
        -nographic \
        > ${local.vm_dir}/vm-console-tf.log 2>&1 &
      echo $! > ${local.vm_dir}/vm-tf.pid
      cat ${local.vm_dir}/vm-tf.pid
    EOT

    delete = <<-EOT
      if [ -f ${local.vm_dir}/vm-tf.pid ]; then
        kill $(cat ${local.vm_dir}/vm-tf.pid) 2>/dev/null || true
        rm -f ${local.vm_dir}/vm-tf.pid
      fi
      pkill -f "qemu.*${var.cluster_name}" || true
    EOT

    read = "cat ${local.vm_dir}/vm-tf.pid 2>/dev/null || echo ''"
  }

  environment = {
    VM_DIR       = local.vm_dir
    CLUSTER_NAME = var.cluster_name
  }
}

# Wait for VM to be ready (maintenance mode)
resource "shell_script" "wait_for_vm" {
  depends_on = [shell_script.start_vm]

  lifecycle_commands {
    create = "sleep 30 && echo 'ready'"
    read   = "echo 'ready'"
    delete = "echo ''"
  }
}

# Apply machine configuration to the VM
resource "talos_machine_configuration_apply" "controlplane" {
  depends_on = [
    shell_script.wait_for_vm
  ]

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.controlplane.machine_configuration
  endpoint                    = "127.0.0.1"
  node                        = "127.0.0.1"
}

# Wait for installation to complete
resource "shell_script" "wait_for_install" {
  depends_on = [talos_machine_configuration_apply.controlplane]

  lifecycle_commands {
    create = "sleep 120 && echo 'installed'"
    read   = "echo 'installed'"
    delete = "echo ''"
  }
}

# Bootstrap the Kubernetes cluster
resource "talos_machine_bootstrap" "this" {
  depends_on = [
    shell_script.wait_for_install
  ]

  client_configuration = talos_machine_secrets.this.client_configuration
  endpoint             = "127.0.0.1"
  node                 = "127.0.0.1"
}

# Wait for cluster to be ready
resource "shell_script" "wait_for_cluster" {
  depends_on = [talos_machine_bootstrap.this]

  lifecycle_commands {
    create = "sleep 90 && echo 'ready'"
    read   = "echo 'ready'"
    delete = "echo ''"
  }
}

# Get cluster kubeconfig
data "talos_cluster_kubeconfig" "this" {
  depends_on = [
    shell_script.wait_for_cluster
  ]

  client_configuration = talos_machine_secrets.this.client_configuration
  endpoint             = "127.0.0.1"
  node                 = "127.0.0.1"
}

# Save kubeconfig to file
resource "local_file" "kubeconfig" {
  content         = data.talos_cluster_kubeconfig.this.kubeconfig_raw
  filename        = "${path.module}/kubeconfig"
  file_permission = "0600"
}

# Save talosconfig to file
resource "local_file" "talosconfig" {
  content         = data.talos_client_configuration.this.talos_config
  filename        = "${path.module}/talosconfig"
  file_permission = "0600"
}
