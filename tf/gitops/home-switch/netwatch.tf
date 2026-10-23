resource "routeros_tool_netwatch" "lan" {
  for_each = var.netwatch_targets

  name        = each.key
  host        = each.value
  src_address = var.switch_address
  type        = "icmp"
  disabled    = false
  comment     = "Switch-originated LAN probe; managed by tf/gitops/home-switch."

  # RouterOS reads 137ms back as 130ms, causing perpetual drift. Use the
  # observed value, retaining a different cadence from smokeping's 200ms.
  interval        = "1m"
  packet_count    = 200
  packet_interval = "130ms"
  packet_size     = 84
  timeout         = "1s"
  start_delay     = "3s"
  startup_delay   = "30s"

  # Retain complete batch statistics, including during a sustained loss episode.
  early_failure_detection = false
  early_success_detection = false
  thr_loss_percent        = 5
  thr_avg                 = "1s"
  thr_max                 = "1s"
  thr_jitter              = "1s"
  thr_stdev               = "1s"
}
