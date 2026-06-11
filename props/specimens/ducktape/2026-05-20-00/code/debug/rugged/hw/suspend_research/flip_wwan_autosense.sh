#!/usr/bin/env bash
# Test hypothesis: WwanAutoSense BIOS setting gates the DSDT's WWEN byte,
# which gates exposure of `_RST` / `MRST._RST` / `_PRR` on
# \_SB.PC00.RP02.PXSX, which gates the kernel's ability to invoke the
# platform's WWAN slot power-cycle (FHRF + SHRF in DSDT).
#
# Effect of flipping: see debug/rugged/hw/modem_suspend_research.md
#   §DSDT decompile findings.
#
# Flow:
#   1. Read current_value (must be "Disabled" for us to flip)
#   2. Check if BIOS admin password is required
#   3. Write "Enabled" → current_value (takes effect at next reboot)
#   4. Re-read to confirm value was accepted
#   5. Print next-step instructions
#
# Rollback: same script with TARGET=Disabled, or just toggle in BIOS Setup.

set -uo pipefail

ATTR=/sys/class/firmware-attributes/dell-wmi-sysman/attributes/WwanAutoSense
AUTH=/sys/class/firmware-attributes/dell-wmi-sysman/authentication/Admin
TARGET="${TARGET:-Enabled}"

die() {
  echo "FATAL: $*" >&2
  exit 1
}
say() { printf '\n========== %s ==========\n' "$*"; }
step() { echo "  $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -d "$ATTR" ] || die "no dell-wmi-sysman WwanAutoSense attribute on this host"

say "before"
cur=$(cat "$ATTR/current_value")
poss=$(cat "$ATTR/possible_values" 2>/dev/null)
step "WwanAutoSense.current_value = $cur"
step "possible_values             = $poss"

if [ "$cur" = "$TARGET" ]; then
  step "already $TARGET — nothing to do."
  exit 0
fi

say "checking BIOS admin password requirement"
if [ -r "$AUTH/is_enabled" ]; then
  is_enabled=$(cat "$AUTH/is_enabled" 2>&1)
  role=$(cat "$AUTH/role" 2>/dev/null || echo "?")
  step "Admin.is_enabled = $is_enabled  role = $role"
  if [ "$is_enabled" = "1" ] || [ "$is_enabled" = "true" ] || [ "$is_enabled" = "enabled" ]; then
    step "Admin password is set — dell-wmi-sysman will reject the write unless"
    step "the password is staged at $AUTH/current_password first."
    step "If the write below returns EACCES, do:"
    step "  echo -n '<your-bios-admin-password>' > $AUTH/current_password"
    step "  and re-run this script."
  fi
fi

say "writing"
step "echo $TARGET > $ATTR/current_value"
if ! echo "$TARGET" >"$ATTR/current_value" 2>err.tmp; then
  rc=$?
  echo "  write failed (rc=$rc):"
  sed 's/^/  /' err.tmp
  rm -f err.tmp
  die "could not flip WwanAutoSense — see error above"
fi
rm -f err.tmp

say "after"
new=$(cat "$ATTR/current_value")
step "WwanAutoSense.current_value = $new"
if [ "$new" != "$TARGET" ]; then
  die "value did not stick (still $new) — BIOS may have rejected the change"
fi

say "DONE"
cat <<MSG
The change is staged in dell-wmi-sysman but the BIOS only applies it at POST.
You must reboot for the new value to take effect.

After reboot, verify the effect with:

  sudo /home/agentydragon/code/ducktape/debug/rugged/modem.sh dump

In the new snapshot, look for:

  ----- PCI reset_method (presence of 'acpi' indicates BIOS WWEN >= 1) -----
  0000:71:00.0: acpi flr bus     <-- if 'acpi' appears, hypothesis confirmed

If 'acpi' is now in the list, the platform's WWAN slot power-cycle
(FHRF/SHRF in DSDT) is exposed to the kernel, and pci_try_reset_function
in mhi_pci_recovery_work will invoke it.

If 'acpi' is NOT in the list (still "flr bus"), WwanAutoSense was not the
gate — the relevant BIOS toggle is somewhere else, possibly in BIOS Setup
behind a "Service" or hidden tab. Capture the DSDT post-reboot
(suspend_research/snapshots/<TS>/DSDT.aml) and grep for WWEN to see
whether the value changed at all.

To revert: TARGET=Disabled $0
MSG
