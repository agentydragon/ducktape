# Ship Talos service logs off-node to the in-cluster Vector receiver.
#
# Talos has no journald; machine.logging.destinations streams newline-delimited JSON to a
# local endpoint. The vector-talos-logs DaemonSet (cluster/k8s/vector-talos-logs/) joins
# the host network and binds only host loopback on :13333, which forwards the logs to
# cluster Loki. The node-vendor=talos label scopes that DaemonSet to Talos nodes (NixOS
# nodes ship their journal via the promtail-journal HelmRelease instead).
#
# machine.logging and machine.nodeLabels are non-reboot fields, applied live. Land the
# DaemonSet before applying this so the endpoint has a listener (see
# debug/atlas/gpu_lockup_20260718_followups.md and the plan deploy order).
#
# Merged (RFC 7386) into every Talos node's config_patches, so node-vendor is added to the
# per-node nodeLabels map rather than replacing it.
locals {
  talos_node_logging_patch = yamlencode({
    machine = {
      nodeLabels = {
        "node-vendor" = "talos"
      }
      logging = {
        destinations = [
          {
            endpoint = "tcp://127.0.0.1:13333"
            format   = "json_lines"
          }
        ]
      }
    }
  })
}
