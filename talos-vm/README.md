# Talos Linux on QEMU

This directory contains two approaches for running Talos Linux Kubernetes clusters on QEMU:

## 🚀 Terraform (Recommended)

**Fully automated, tested end-to-end, ready to use.**

Location: [`terraform/`](terraform/)

- ✅ One command deployment (`terraform apply`)
- ✅ ~5-6 minutes from zero to working cluster
- ✅ Declarative infrastructure as code
- ✅ Proper cleanup with `terraform destroy`
- ✅ Tested and documented

**Start here:** [`terraform/README.md`](terraform/README.md)

## 🔧 Manual Setup

**Scripts and documentation for manual setup process.**

Location: [`manual/`](manual/)

- Step-by-step scripts for understanding the process
- Complete documentation of all workarounds
- Useful for learning or customization
- Requires manual intervention at each step

**Start here:** [`manual/SETUP.md`](manual/SETUP.md)

## Quick Comparison

| Feature | Terraform | Manual |
|---------|-----------|--------|
| Setup Time | ~5-6 minutes | ~20-30 minutes |
| Automation | Fully automated | Manual steps |
| Repeatability | Declarative | Script-based |
| Cleanup | `terraform destroy` | Manual cleanup |
| Learning Curve | Moderate | Detailed |

## Prerequisites

Both approaches require:
- QEMU (qemu-system-x86_64)
- DNS-over-HTTPS proxy (cloudflared)
- Authenticated HTTPS proxy (for container registries)

See the respective READMEs for detailed setup instructions.
