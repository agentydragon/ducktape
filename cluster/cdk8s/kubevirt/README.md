# KubeVirt

`operators.py` installs virt-operator and cdi-operator from their upstream release manifests: each generated
`kustomization.yaml` names the pinned release URL and patches it, so no upstream YAML is vendored. Kustomize fetches the
URL unverified; the assets' SHA-256 pins are `MODULE.bazel`'s, for the CRD bindings. Renovate does not track the URLs:
its kustomize manager reads a remote resource's version only from a `?ref=` or `?version=` query.

VM workloads run on non-control-plane workers in regions `hil`, `home` and `proxmox` (`app.py`'s
`workloads.nodePlacement`). `wyrm2` (region `proxmox`) is a Proxmox guest with nested KVM: `kvm_amd` `nested=1` on both
`atlas` (L0) and `wyrm2` (L1).
