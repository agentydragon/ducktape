# SPICE guest tooling for VM guests with a display attached.
#
# Split out of vm-hardware.nix, which every VM imports: the headless guests (agent-box,
# public-coder-devbox, gecko, bootstrap, cpap-gateway) were running spice-vdagentd and carrying a
# user unit waiting on a graphical-session.target that never arrives.
{ pkgs, ... }:
{
  # Clipboard sharing + display resize. Display is QXL-driven (NVIDIA GPUs are headless compute
  # via VFIO). Resize works via QXL DRM hotplug -> mutter (GNOME handles it natively).
  # spice-autorandr is X11-only and not needed with GNOME/Wayland.
  services.spice-vdagentd.enable = true;

  # The NixOS module only starts spice-vdagentd (system daemon). The per-user spice-vdagent
  # process is also needed for display resize and clipboard. It ships an XDG autostart .desktop,
  # but GNOME 49 ignores it (X-GNOME-Autostart-Phase is no longer honored), hence a systemd user
  # service. Clipboard sharing is broken on Wayland (upstream limitation, not NixOS).
  # See: https://github.com/NixOS/nixpkgs/issues/481078
  # TODO: Remove this once nixpkgs merges PR #266080 or equivalent upstream fix.
  systemd.user.services.spice-vdagent = {
    description = "SPICE guest agent (user session)";
    wantedBy = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.spice-vdagent}/bin/spice-vdagent -x";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };
}
