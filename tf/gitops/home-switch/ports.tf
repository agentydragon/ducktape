# Verified against the switch's learned MAC addresses and the gateway's device list.
# Keep factory interface names stable; comments also appear in mktxp's interface metadata.
resource "routeros_interface_ethernet" "connected" {
  for_each = {
    ether1 = "Gateway uplink (AT&T BGW320)"
    ether3 = "OptiPlex"
    ether4 = "Atlas (Proxmox; wyrm2)"
    ether5 = "Airthings hub"
  }

  factory_name = each.key
  name         = each.key
  comment      = each.value

  # CLEANUP(#9604): restore 2.5G advertisement for the replacement-cable comparison.
  advertise        = each.key == "ether1" ? "1G-baseT-full" : null
  auto_negotiation = each.key == "ether1" ? true : null

  lifecycle {
    prevent_destroy = true
  }
}
