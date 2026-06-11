#!/usr/bin/env bash
# Foxconn DW5934e (Dell DW5934e, SDX72) modem operations + diagnostics.
#
# Subcommands:
#   status                       Print modem/SIM/slot state. Read-only, fast.
#   diagnose [--kill-wifi]       End-to-end bring-up + diagnostic + auto-recovery.
#                                Default keeps WiFi up (cellular tests bind to wwan0).
#                                --kill-wifi exercises the WiFi-down failover path.
#                                Writes to debug/rugged-mobile-net-diag/<TS>/.
#   slot <0|1>                   Switch active SIM slot (mbim 0 = physical, 1 = eSIM).
#                                Stops/starts MM; safe with WiFi up.
#   esim status                  List eUICC profiles + UICC applications.
#   esim wipe                    Delete all eUICC profiles. Verifies eUICC is empty.
#   esim activate <qr|LPA>       Wipe + download new profile + bring modem online.
#                                Arg is QR-image path or LPA:1$smdp$matching-id string.
#   unlock                       Manually run FoxFlss FCC unlock + restart MM. Use
#                                when foxflss-watchdog isn't enough (rare).
#   try-5g                       Switch modem to allowed=4g|5g preferred=5g, then
#                                validate connectivity (state=connected + ping +
#                                1MB HTTP). Auto-reverts to allowed=4g on any
#                                failure or interrupt (Ctrl-C). Safe to run.
#   recover                      Attempt non-reboot modem firmware recovery sequence
#                                (MHI rebind → PCI remove/rescan → module reload).
#                                Prints suspend-resume suggestion if all fail.
#   dump                         Snapshot all forensic state to
#                                debug/rugged/hw/suspend_research/snapshots/<TS>/:
#                                full dmesg, full MM + kernel journal since boot,
#                                modem dumps (mmcli, mbimcli), sysfs state,
#                                ACPI wakeup table, lspci, /sys/power caps.
#                                Read-only, safe with WiFi up, works on wedged modem.
#
# Requires: lpac, mbimcli, FoxFlss, mmcli, nmcli, zbarimg (for QR decode); root.
#
# Replaces the earlier `cellular_diag.sh` and `esim_provision.sh`.

set -uo pipefail
require_root() {
  [ "$(id -u)" -eq 0 ] || {
    echo "must run as root" >&2
    exit 1
  }
}

# ─────────────────────────── constants ────────────────────────────────────
REPO=/home/agentydragon/code/ducktape
PCI_ADDR=0000:71:00.0
MHI_DRIVER=/sys/bus/pci/drivers/mhi-pci-generic
MBIM_DEV=/dev/wwan0mbim0
WIFI_DEV=wlp0s20f3
WIFI_CONN=Howleroi
GSM_CONN="Google Fi"

# ─────────────────────────── logging ──────────────────────────────────────
say() { printf '\n========== %s ==========\n' "$*"; }
hdr() { printf '\n----- %s -----\n' "$*"; }
step() { echo "  $*"; }
die() {
  echo "FATAL: $*" >&2
  exit 1
}

# ─────────────────────────── modem state probes ───────────────────────────
modem_id() { mmcli -L 2>/dev/null | grep -oE '/Modem/[0-9]+' | head -1 | sed 's:/Modem/::'; }
modem_field() {
  # $1 = field name (e.g. modem.generic.state), $2 = modem id
  mmcli -m "${2:-$(modem_id)}" -K 2>/dev/null \
    | awk -F: -v k="$1" '$1 ~ "^"k"[[:space:]]*$" {gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2}'
}
modem_state() { modem_field modem.generic.state "${1:-}"; }
modem_power() { modem_field modem.generic.power-state "${1:-}"; }
sim_id() {
  mmcli -m "${1:-$(modem_id)}" 2>/dev/null \
    | grep 'primary sim path' \
    | grep -oE '/SIM/[0-9]+' \
    | head -1 \
    | sed 's:/SIM/::'
}

# ─────────────────────────── reset / unlock primitives ────────────────────
mhi_rebind() {
  step "MHI driver rebind…"
  echo "$PCI_ADDR" >"$MHI_DRIVER/unbind" 2>/dev/null || true
  sleep 3
  echo "$PCI_ADDR" >"$MHI_DRIVER/bind" 2>/dev/null || true
  sleep 8
}

# Run lpac with the env this modem needs.
lpac_run() {
  LPAC_APDU=mbim \
    LPAC_APDU_MBIM_DEVICE="$MBIM_DEV" \
    LPAC_APDU_MBIM_UIM_SLOT=2 \
    lpac "$@"
}

# Bring modem from low/disabled to registered. Used after slot switch / esim ops /
# manual unlock. Idempotent — safe to call when modem is already up.
# Returns 0 once MM detects the modem, 1 otherwise (callers like cmd_recover
# rely on this NOT die()ing so they can fall through to the next recovery step).
bring_modem_online() {
  hdr "bring modem online"
  systemctl start ModemManager 2>/dev/null || true
  sleep 10
  # FCC unlock is handled automatically by MM's wired fcc-unlock.d (installed by
  # nix/nixos/hosts/rugged/foxconn-wwan.nix with the absolute foxflss store path)
  # when MM hits "software radio switch is OFF" during enable. We only call
  # FoxFlss directly as a fallback IF it happens to be on PATH (e.g. running
  # inside the nix devshell) — under plain sudo it isn't, and that's fine.
  if command -v FoxFlss >/dev/null 2>&1; then
    step "FCC unlock (FoxFlss on PATH)…"
    FoxFlss 2>&1 || step "FoxFlss exit=$? (may be OK if already unlocked)"
  else
    step "FoxFlss not on PATH — relying on MM's wired fcc-unlock.d (auto)"
  fi
  step "restart MM to pick up new power state…"
  systemctl restart ModemManager
  # Poll up to 60s: the unlock → enable → register → connect sequence takes
  # tens of seconds after a remove/rescan; a fixed sleep raced ahead of it.
  hdr "wait up to 60s for MM to detect + bring up the modem"
  wait_for_state 60 registered enabled connected connecting || true
  local m
  m=$(modem_id)
  if [ -z "$m" ]; then
    step "ModemManager has not detected a modem yet"
    return 1
  fi
  step "modem state: $(modem_state "$m") / power: $(modem_power "$m")"
  return 0
}

# Wait for modem to reach a target state (or a list of states).
# $1 = max seconds, rest = acceptable states.
wait_for_state() {
  local max=$1
  shift
  for i in $(seq 1 "$max"); do
    local m s
    m=$(modem_id)
    s=$(modem_state "$m")
    printf '  [%2ds] modem=%s state=%s\n' "$i" "${m:-?}" "${s:-?}"
    for target in "$@"; do
      [ "$s" = "$target" ] && return 0
    done
    sleep 1
  done
  return 1
}

# ─────────────────────────── status ────────────────────────────────────────
# Print MHI/PCI bus state for the modem. This is the layer BELOW ModemManager:
# if MHI channels haven't enumerated, MM cannot detect the modem and nothing
# else in cmd_status will be meaningful. Distinguishes "PCI device gone"
# (whole device missing, e.g. failed link train after suspend) from "MHI
# wedged" (PCI device present, driver bound, but no mhi0_* channels — modem
# firmware never completed handshake; classic suspend/resume wedge symptom).
mhi_status() {
  hdr "MHI / PCI ($PCI_ADDR)"
  if [ ! -e "/sys/bus/pci/devices/$PCI_ADDR" ]; then
    step "PCI device MISSING — link train failed or device removed"
    return
  fi
  local drv rt ctrl chans
  drv=$(basename "$(readlink "/sys/bus/pci/devices/$PCI_ADDR/driver" 2>/dev/null)" 2>/dev/null || echo none)
  rt=$(cat "/sys/bus/pci/devices/$PCI_ADDR/power/runtime_status" 2>/dev/null || echo ?)
  ctrl=$(cat "/sys/bus/pci/devices/$PCI_ADDR/power/control" 2>/dev/null || echo ?)
  step "driver=$drv runtime_status=$rt power/control=$ctrl"
  if [ -d "/sys/bus/pci/devices/$PCI_ADDR/mhi0" ]; then
    chans=$(find "/sys/bus/pci/devices/$PCI_ADDR/mhi0" -maxdepth 1 -name 'mhi0_*' -printf '%f ' 2>/dev/null)
    if [ -n "$chans" ]; then
      step "mhi0 channels: $chans"
    else
      step "mhi0 channels: NONE — firmware handshake never completed (modem wedged)"
    fi
  else
    step "no mhi0 sysfs node — MHI controller not probed"
  fi
  local wwan_devs
  wwan_devs=$(ls /dev/wwan0* 2>/dev/null | tr '\n' ' ')
  step "/dev/wwan0*: ${wwan_devs:-<none>}"
}

# Show foxflss-watchdog state. If the watchdog is hot-respawning (mmcli -w
# exiting in a tight loop), the modem is gone from MM's view entirely and the
# watchdog is stuck — that's a secondary symptom of an MHI wedge, not a cause.
foxflss_watchdog_status() {
  hdr "foxflss-watchdog"
  if ! systemctl list-unit-files foxflss-watchdog.service >/dev/null 2>&1; then
    step "unit not present"
    return
  fi
  step "active: $(systemctl is-active foxflss-watchdog.service 2>&1)"
  local respawns
  respawns=$(journalctl -u foxflss-watchdog --since '1 min ago' --no-pager 2>/dev/null \
    | grep -c 'respawn' || true)
  if [ "$respawns" -gt 10 ]; then
    step "HOT-SPINNING: $respawns respawns in last 60s (mmcli -w exiting; no modem to wait on)"
  else
    step "respawns in last 60s: $respawns"
  fi
  journalctl -u foxflss-watchdog --since '30 sec ago' --no-pager 2>/dev/null \
    | tail -3 | sed 's/^/    /'
}

# Filtered kernel + MM log slice anchored to the most recent suspend/resume.
# This is what tells you WHY the modem is in its current state — typically
# `mhi_pci_suspend ... ret -16` (EBUSY) or `MHI: Forcibly moving from M3 to M0`.
# Excludes foxflss-watchdog respawn spam (covered in its own section above).
recent_kernel_modem_log() {
  local last_resume since
  last_resume=$(journalctl --no-pager -o short-iso 2>/dev/null \
    | grep -E 'systemd-sleep.*(Entering|Woke up)|kernel.*PM: (suspend exit|resume)' \
    | tail -1)
  if [ -n "$last_resume" ]; then
    hdr "last suspend/resume marker"
    echo "  $last_resume"
    since=$(echo "$last_resume" | awk '{print $1}')
  fi
  hdr "kernel + MM events since last resume (excl. watchdog spam)"
  journalctl --since "${since:-30 min ago}" --no-pager 2>/dev/null \
    | grep -iE 'mhi|wwan|cdc_mbim|fcc-unlock|0000:71:00|modemmanager' \
    | grep -vE 'foxflss-watchdog.*respawn' \
    | tail -30 || true
}

cmd_status() {
  mhi_status
  foxflss_watchdog_status
  hdr "modem"
  local m
  m=$(modem_id)
  if [ -n "$m" ]; then
    mmcli -m "$m" 2>&1 | grep -E 'state:|power|signal|access|operator' | head -20
    local s
    s=$(sim_id "$m")
    if [ -n "$s" ]; then
      hdr "active SIM"
      mmcli -i "$s" 2>&1 | grep -E 'iccid|imsi|operator|active'
    fi
  else
    step "no modem detected by ModemManager"
  fi
  hdr "slot mapping (mbim)"
  if [ -e "$MBIM_DEV" ]; then
    mbimcli -d "$MBIM_DEV" -p --ms-query-device-slot-mappings 2>&1 || true
    for slot in 0 1; do
      mbimcli -d "$MBIM_DEV" -p --ms-query-slot-info-status="$slot" 2>&1 || true
    done
  else
    step "$MBIM_DEV missing — skipping MBIM probes (see MHI / PCI above)"
  fi
  hdr "wwan0 IP"
  ip -4 -o addr show wwan0 2>/dev/null || step "no wwan0 device"
  hdr "Google Fi NM connection"
  nmcli -t -f GENERAL.STATE,IP4.ADDRESS connection show "$GSM_CONN" 2>&1 \
    | head -5 || true
  recent_kernel_modem_log
}

# ─────────────────────────── slot switch ───────────────────────────────────
cmd_slot() {
  local target="${1:-}"
  case "$target" in
    0 | 1) ;;
    *) die "usage: $0 slot <0|1>  (0 = physical, 1 = eSIM)" ;;
  esac
  hdr "stop ModemManager"
  systemctl stop ModemManager
  sleep 2
  hdr "set slot mapping = $target"
  mbimcli -d "$MBIM_DEV" --ms-set-device-slot-mappings="$target" 2>&1 || true
  sleep 2
  hdr "start ModemManager"
  systemctl start ModemManager
  hdr "wait up to 90s for re-probe"
  wait_for_state 90 registered enabled connected connecting disabled \
    || step "modem not registered within 90s — see 'unlock' or 'recover'"
}

# ─────────────────────────── manual unlock ─────────────────────────────────
cmd_unlock() {
  hdr "manual FCC unlock + MM restart"
  bring_modem_online
}

# ─────────────────────────── eSIM lifecycle ────────────────────────────────
verify_lpac_access() { lpac_run chip info >/dev/null 2>&1; }

verify_euicc_empty() {
  hdr "verify eUICC is empty"
  step "checking UICC application list (mbimcli)…"
  local apps
  apps=$(mbimcli -d "$MBIM_DEV" --ms-query-uicc-application-list 2>&1 || true)
  if echo "$apps" | grep -qi 'usim\|isim'; then
    echo "$apps"
    die "eUICC still has active applications — wipe incomplete"
  fi
  step "no USIM/ISIM apps."
  local list
  list=$(lpac_run profile list 2>&1 || true)
  if echo "$list" | grep -q '"iccid"'; then
    echo "$list"
    die "lpac still sees profiles — wipe incomplete"
  fi
  step "verified: eUICC clean."
}

verify_profile_active() {
  hdr "verify profile is active"
  local apps
  apps=$(mbimcli -d "$MBIM_DEV" --ms-query-uicc-application-list 2>&1 || true)
  if echo "$apps" | grep -qi 'usim'; then
    step "USIM application present — profile is active."
    echo "$apps" | grep -i 'application'
  else
    echo "$apps"
    die "no USIM application — profile may not be enabled"
  fi
}

cmd_esim_status() {
  cmd_status
  hdr "UICC applications (mbimcli)"
  mbimcli -d "$MBIM_DEV" -p --ms-query-uicc-application-list 2>&1 || true
  hdr "eUICC profiles (lpac)"
  step "stopping ModemManager for lpac access…"
  systemctl stop ModemManager 2>/dev/null || true
  sleep 2
  mhi_rebind
  lpac_run profile list 2>&1 || step "lpac profile list failed"
  step "restarting MM and unlocking…"
  mhi_rebind
  bring_modem_online
}

cmd_esim_wipe() {
  hdr "init modem MBIM stack"
  systemctl start ModemManager 2>/dev/null || true
  sleep 8
  systemctl stop ModemManager 2>/dev/null || true
  sleep 2
  hdr "MHI rebind (clean state for lpac)"
  mhi_rebind
  if ! verify_lpac_access; then
    step "lpac can't access eUICC — second rebind with longer wait…"
    echo "$PCI_ADDR" >"$MHI_DRIVER/unbind" 2>/dev/null || true
    sleep 5
    echo "$PCI_ADDR" >"$MHI_DRIVER/bind" 2>/dev/null || true
    sleep 15
    verify_lpac_access || die "cannot access eUICC after two rebinds"
  fi
  hdr "list existing profiles"
  local pjson
  pjson=$(lpac_run profile list 2>&1)
  echo "$pjson"
  local iccids
  iccids=$(echo "$pjson" | grep -oE '"iccid":"[^"]+"' | cut -d'"' -f4 || true)
  if [ -z "$iccids" ]; then
    step "no profiles — eUICC already clean."
    verify_euicc_empty
    return 0
  fi
  hdr "deleting all profiles"
  local iccid state result
  for iccid in $iccids; do
    state=$(echo "$pjson" \
      | grep -oE "\"iccid\":\"$iccid\"[^}]*\"profileState\":\"[^\"]+\"" \
      | grep -oE '"profileState":"[^"]+"' | cut -d'"' -f4)
    step "profile $iccid ($state)"
    if [ "$state" = "enabled" ]; then
      step "  disabling…"
      result=$(lpac_run profile disable "$iccid" 2>&1)
      echo "$result" | grep -q '"code":0' && step "  disabled." || step "  $result"
      step "  MHI rebind (SIM reset after disable)…"
      mhi_rebind
    fi
    step "  deleting…"
    result=$(lpac_run profile delete "$iccid" 2>&1)
    if ! echo "$result" | grep -q '"code":0'; then
      step "  delete failed, MHI rebind + retry…"
      mhi_rebind
      result=$(lpac_run profile delete "$iccid" 2>&1)
      echo "$result" | grep -q '"code":0' || die "could not delete $iccid: $result"
    fi
    step "  deleted."
  done
  mhi_rebind
  verify_euicc_empty
}

cmd_esim_activate() {
  local input="${1:-}"
  [ -n "$input" ] || die "usage: $0 esim activate <qr-image.png | LPA:1\$smdp\$matching-id>"
  local lpa
  if [[ "$input" == LPA:* ]]; then
    lpa="$input"
  else
    [ -f "$input" ] || die "file not found: $input"
    command -v zbarimg >/dev/null || die "zbarimg not found (install zbar)"
    lpa=$(zbarimg --raw "$input" 2>/dev/null) || die "QR decode failed: $input"
  fi
  [[ "$lpa" == LPA:1\$* ]] || die "not a valid LPA string: $lpa"
  local smdp matching
  smdp=$(echo "$lpa" | cut -d'$' -f2)
  matching=$(echo "$lpa" | cut -d'$' -f3)
  echo "SM-DP+:      $smdp"
  echo "Matching ID: $matching"

  cmd_esim_wipe

  hdr "download new profile"
  step "this can take 30-60s…"
  mhi_rebind
  local out
  out=$(lpac_run profile download -s "$smdp" -m "$matching" 2>&1)
  echo "$out"
  if echo "$out" | grep -q '"code":0.*"message":"success"'; then
    step "download succeeded."
  elif echo "$out" | grep -q 'iccid_already_exists_on_euicc'; then
    step "profile already installed (previous attempt succeeded)."
  elif echo "$out" | grep -q 'Transaction timed out'; then
    step "timed out — checking if profile installed anyway…"
    mhi_rebind
    local apps
    apps=$(mbimcli -d "$MBIM_DEV" --ms-query-uicc-application-list 2>&1 || true)
    echo "$apps" | grep -qi 'usim' \
      || die "download timed out, no USIM app present"
    step "profile IS installed despite timeout."
  else
    die "download failed: $out"
  fi

  mhi_rebind
  verify_profile_active

  bring_modem_online

  hdr "lock to LTE (5G NR unusable at primary location)"
  local m
  m=$(modem_id)
  mmcli -m "$m" --set-allowed-modes=4g 2>&1 || true
  sleep 3

  hdr "wait for wwan0 IPv4"
  for i in $(seq 1 20); do
    ip -4 addr show wwan0 2>/dev/null | grep -q 'inet ' && break
    sleep 2
  done
  ip -4 addr show wwan0 2>/dev/null | grep inet || step "no IPv4 yet"

  hdr "ping + throughput sanity check"
  ping -I wwan0 -c 3 -W 5 8.8.8.8 2>&1 || step "ping failed"
  curl --interface wwan0 -4 -o /dev/null -sm 20 \
    -w "  speed: %{speed_download} B/s  size: %{size_download} bytes  time: %{time_total}s\n" \
    http://speedtest.tele2.net/1MB.zip 2>&1 || step "download timed out"
  echo
  step "if throughput is ~7 KB/s, the IMEI throttle is in effect (see modem.md TODO)."
}

cmd_esim() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    status) cmd_esim_status ;;
    wipe) cmd_esim_wipe ;;
    activate) cmd_esim_activate "$@" ;;
    *) die "usage: $0 esim {status|wipe|activate}" ;;
  esac
}

# ─────────────────────────── try-5g (auto-revert) ─────────────────────────
# Switch the modem to allow 5G NR (preferred 5G) and verify connectivity.
# Uses an EXIT trap so any failure path (mode-set rejected, no `connected`,
# ping fails, HTTP fails, Ctrl-C) reverts to allowed=4g. Globals (not local
# vars) are required because the trap fires after the function frame is gone.
TRY5G_VALIDATED=0
TRY5G_MODEM_ID=""

# Tear the GSM bearer down, change modes, wait for re-registration, bring it
# back up. Surfaces nmcli failures rather than swallowing them. Used by both
# the try and revert paths so they're symmetric.
try5g_switch_modes() {
  # $1 = label for logs, rest = mmcli mode flags
  local label=$1
  shift
  hdr "down GSM connection (clean tear-down before mode change)"
  nmcli connection down "$GSM_CONN" 2>&1 \
    || step "nmcli down returned $? (already down?)"
  sleep 2
  hdr "switching to $label"
  mmcli -m "$TRY5G_MODEM_ID" "$@" 2>&1 || {
    step "mmcli mode set failed (rc=$?)"
    return 1
  }
  hdr "wait up to 90s for registered/connected"
  wait_for_state 90 registered connected || {
    step "modem did not re-register within 90s"
    return 1
  }
  hdr "bring up '$GSM_CONN' (will surface real errors)"
  nmcli connection up "$GSM_CONN" 2>&1 || {
    step "nmcli connection up failed (rc=$?)"
    return 1
  }
  hdr "wait up to 15s for wwan0 IPv4"
  local i
  for i in $(seq 1 15); do
    ip -4 -o addr show wwan0 2>/dev/null | grep -q 'inet ' && break
    sleep 1
  done
  ip -4 -o addr show wwan0 2>&1 | grep -q 'inet ' || {
    step "no wwan0 IPv4 after 15s"
    return 1
  }
  ip -4 -o addr show wwan0 2>&1 | head -1
}

try5g_revert_to_lte() {
  [ "$TRY5G_VALIDATED" = 1 ] && return 0
  [ -z "$TRY5G_MODEM_ID" ] && return 0
  say "REVERT — falling back to allowed=4g"
  try5g_switch_modes "allowed=4g" --set-allowed-modes=4g \
    || step "revert had errors — modem may need manual recovery"
  hdr "post-revert state"
  mmcli -m "$TRY5G_MODEM_ID" 2>&1 \
    | grep -E 'state:|access tech|signal quality' | head -5
}

cmd_try_5g() {
  TRY5G_MODEM_ID=$(modem_id)
  [ -n "$TRY5G_MODEM_ID" ] || die "no modem detected"

  hdr "pre-state"
  step "current state: $(modem_state "$TRY5G_MODEM_ID")"
  mmcli -m "$TRY5G_MODEM_ID" 2>&1 \
    | grep -E 'access tech|signal quality|current:' | head -5

  trap 'try5g_revert_to_lte' EXIT

  # mmcli 1.24 requires --set-allowed-modes + --set-preferred-mode in one call.
  try5g_switch_modes "allowed=4g|5g, preferred=5g" \
    --set-allowed-modes='4g|5g' --set-preferred-mode=5g || {
    step "switch to 5G failed — trap will revert"
    return 1
  }

  hdr "post-switch modem"
  mmcli -m "$TRY5G_MODEM_ID" 2>&1 \
    | grep -E 'state:|access tech|signal quality|operator name' | head -8

  hdr "ICMP probe (3 packets via wwan0)"
  ping -I wwan0 -c 3 -W 5 8.8.8.8 || {
    step "ping failed — trap will revert"
    return 1
  }

  hdr "HTTP probe (1MB tele2, 20s budget)"
  curl --interface wwan0 -4 -sm 20 -o /dev/null \
    -w "  speed=%{speed_download}B/s time=%{time_total}s code=%{http_code}\n" \
    http://speedtest.tele2.net/1MB.zip || {
    step "download failed — trap will revert"
    return 1
  }

  TRY5G_VALIDATED=1
  say "5G mode is working — keeping it"
  mmcli -m "$TRY5G_MODEM_ID" 2>&1 \
    | grep -E 'access tech|signal quality' | head -2
  step "manual revert: mmcli -m $TRY5G_MODEM_ID --set-allowed-modes=4g"
}

# ─────────────────────────── recovery ──────────────────────────────────────
# Attempt to recover a wedged modem firmware (e.g. after a SYS ERROR, or
# the post-eSIM-download wedge) without rebooting. Tries escalating resets.
# Returns success if mmcli sees the modem after; failure prints next steps.
cmd_recover() {
  local m s
  m=$(modem_id)
  if [ -n "$m" ]; then
    s=$(modem_state "$m")
    case "$s" in
      registered | connected)
        step "modem already $s — nothing to do."
        return 0
        ;;
    esac
  fi

  hdr "step 1/3: MHI driver rebind"
  systemctl stop ModemManager 2>/dev/null || true
  sleep 2
  mhi_rebind
  ls /dev/wwan0* >/dev/null 2>&1 && {
    bring_modem_online
    [ -n "$(modem_id)" ] && return 0
  }

  hdr "step 2/3: PCI remove + bus rescan"
  echo 1 >"/sys/bus/pci/devices/$PCI_ADDR/remove" 2>/dev/null
  sleep 2
  echo 1 >/sys/bus/pci/rescan
  sleep 8
  ls /dev/wwan0* >/dev/null 2>&1 && {
    bring_modem_online
    [ -n "$(modem_id)" ] && return 0
  }

  hdr "step 3/3: module reload"
  rmmod mhi_wwan_mbim mhi_wwan_ctrl mhi_pci_generic mhi 2>/dev/null || true
  echo 1 >"/sys/bus/pci/devices/$PCI_ADDR/remove" 2>/dev/null
  sleep 2
  echo 1 >/sys/bus/pci/rescan
  sleep 3
  modprobe mhi
  modprobe mhi_pci_generic
  modprobe mhi_wwan_mbim
  sleep 8
  ls /dev/wwan0* >/dev/null 2>&1 && {
    bring_modem_online
    [ -n "$(modem_id)" ] && return 0
  }

  cat <<'MSG'

All non-reboot recovery steps failed. The modem firmware is wedged at the
SBL stage (`No firmware image defined` in dmesg) and needs the PCIe slot
to physically lose power. Two options:

  1. systemctl suspend; then resume — power-cycles the slot via S3.
     (Caveat: mhi_pci_suspend EBUSY is a known open bug; may itself fail.)
  2. systemctl reboot — reliable.

Recent dmesg lines:

MSG
  dmesg | grep -iE 'mhi|wwan|71:00' | tail -10
  return 1
}

# ─────────────────────────── forensic dump ────────────────────────────────
# Snapshot every observable bit of state into a timestamped dir under
# debug/rugged/hw/suspend_research/snapshots/<TS>/. Designed for the
# suspend/resume root-cause investigation (see modem_suspend_research.md).
# Read-only, safe with WiFi up, works on a wedged modem.
cmd_dump() {
  local ts out
  ts=$(date +%Y%m%d-%H%M%S)
  out="$REPO/debug/rugged/hw/suspend_research/snapshots/$ts"
  mkdir -p "$out"
  exec > >(tee "$out/00_dump.log") 2>&1
  hdr "forensic dump → $out"

  hdr "kernel"
  uname -a
  cat /proc/cmdline
  echo
  echo "boot time: $(uptime -s)   uptime: $(uptime -p)"

  hdr "/sys/power capabilities"
  for f in /sys/power/mem_sleep /sys/power/state /sys/power/disk \
    /sys/power/wakeup_count /sys/power/pm_test; do
    echo -n "$f: "
    cat "$f" 2>&1
  done

  hdr "loaded modem-related modules"
  lsmod | head -1
  lsmod | grep -iE 'mhi|wwan|cdc_mbim|cdc_ncm|cdc_wdm'

  hdr "rfkill list"
  rfkill list

  hdr "/proc/acpi/wakeup"
  cat /proc/acpi/wakeup 2>&1

  hdr "lspci modem ($PCI_ADDR)"
  lspci -vvv -s "$PCI_ADDR" 2>&1

  hdr "lspci parent (00:1c.0)"
  lspci -vvv -s 0000:00:1c.0 2>&1

  hdr "PCI device sysfs (modem)"
  for f in power_state d3cold_allowed enable revision class vendor device \
    subsystem_vendor subsystem_device current_link_speed \
    current_link_width max_link_speed max_link_width; do
    [ -r "/sys/bus/pci/devices/$PCI_ADDR/$f" ] || continue
    echo -n "  $f: "
    cat "/sys/bus/pci/devices/$PCI_ADDR/$f"
  done
  echo "  power/:"
  for f in /sys/bus/pci/devices/$PCI_ADDR/power/*; do
    [ -f "$f" ] || continue
    echo -n "    $(basename "$f"): "
    cat "$f" 2>&1
  done
  echo "  link/:"
  for f in /sys/bus/pci/devices/$PCI_ADDR/link/*; do
    [ -f "$f" ] || continue
    echo -n "    $(basename "$f"): "
    cat "$f" 2>&1
  done

  hdr "ACPI firmware_node (modem)"
  echo -n "  path: "
  cat "/sys/bus/pci/devices/$PCI_ADDR/firmware_node/path" 2>&1
  echo -n "  power_state: "
  cat "/sys/bus/pci/devices/$PCI_ADDR/firmware_node/power_state" 2>&1
  echo "  power/:"
  for f in /sys/bus/pci/devices/$PCI_ADDR/firmware_node/power/*; do
    [ -f "$f" ] || continue
    echo -n "    $(basename "$f"): "
    cat "$f" 2>&1
  done

  hdr "MHI sysfs tree"
  if [ -d "/sys/bus/pci/devices/$PCI_ADDR/mhi0" ]; then
    find "/sys/bus/pci/devices/$PCI_ADDR/mhi0" -maxdepth 2 -printf '%p\n' 2>&1
  else
    echo "  no mhi0 node (driver not bound or modem wedged pre-channel-enum)"
  fi

  hdr "wwan class"
  ls -la /sys/class/wwan/ 2>&1

  hdr "modem.sh status output"
  cmd_status 2>&1

  hdr "mmcli -L"
  mmcli -L 2>&1
  local mid
  mid=$(modem_id)
  if [ -n "$mid" ]; then
    hdr "mmcli -m $mid (human)"
    mmcli -m "$mid" 2>&1
    hdr "mmcli -m $mid -K (key=value)"
    mmcli -m "$mid" -K 2>&1
    local sim
    sim=$(sim_id "$mid")
    if [ -n "$sim" ]; then
      hdr "mmcli -i $sim (active SIM)"
      mmcli -i "$sim" 2>&1
      hdr "mmcli -i $sim -K"
      mmcli -i "$sim" -K 2>&1
    fi
    hdr "mmcli -m $mid --signal-get"
    mmcli -m "$mid" --signal-setup=5 2>&1 || true
    sleep 6
    mmcli -m "$mid" --signal-get 2>&1 || true
    hdr "mmcli -m $mid --location-status"
    mmcli -m "$mid" --location-status 2>&1 || true
  else
    step "no modem detected — skipping mmcli per-modem dumps"
  fi

  if [ -e "$MBIM_DEV" ]; then
    hdr "mbimcli queries"
    for q in --query-device-caps --query-device-services \
      --query-subscriber-ready-status --query-radio-state \
      --query-pin-state --query-home-provider \
      --query-register-state --query-signal-state \
      --query-packet-service-state '--query-connection-state=0' \
      --ms-query-device-slot-mappings \
      '--ms-query-slot-info-status=0' '--ms-query-slot-info-status=1' \
      --ms-query-uicc-application-list; do
      echo "=== mbimcli $q ==="
      eval mbimcli -d "$MBIM_DEV" -p $q 2>&1
      echo
    done
  else
    step "no $MBIM_DEV — skipping mbimcli queries"
  fi

  hdr "networking"
  ip -d link show 2>&1
  echo
  ip addr 2>&1
  echo
  ip -4 route 2>&1
  echo
  ip -6 route 2>&1
  echo
  ip rule 2>&1

  # Wrap nmcli calls in `time` to surface whichever step is slow. The
  # bare `nmcli connection show "$GSM_CONN"` has been observed to take
  # multiple seconds in past dumps despite normally being sub-second —
  # explicit timings will point at whether it's the listing, the per-
  # connection fetch, or something queued behind a busy NM/MM.
  hdr "NM state (each command timed)"
  time nmcli device status 2>&1
  echo
  time nmcli connection show 2>&1
  echo
  time nmcli connection show "$GSM_CONN" 2>&1 || true

  hdr "ModemManager unit"
  systemctl status ModemManager.service --no-pager 2>&1 | head -30
  echo
  systemctl cat ModemManager.service --no-pager 2>&1

  hdr "foxflss-watchdog unit"
  systemctl status foxflss-watchdog.service --no-pager 2>&1 | head -20

  say "writing log files (this can take a moment)"
  dmesg >"$out/dmesg_full.txt" 2>&1
  step "dmesg_full.txt: $(wc -l <"$out/dmesg_full.txt") lines"

  journalctl -k -b 0 --no-pager >"$out/journal_kernel_thisboot.txt" 2>&1
  step "journal_kernel_thisboot.txt: $(wc -l <"$out/journal_kernel_thisboot.txt") lines"

  journalctl -u ModemManager.service -b 0 --no-pager >"$out/journal_mm_thisboot.txt" 2>&1
  step "journal_mm_thisboot.txt: $(wc -l <"$out/journal_mm_thisboot.txt") lines"

  journalctl -u NetworkManager.service -b 0 --no-pager >"$out/journal_nm_thisboot.txt" 2>&1
  step "journal_nm_thisboot.txt: $(wc -l <"$out/journal_nm_thisboot.txt") lines"

  journalctl -u foxflss-watchdog.service -b 0 --no-pager >"$out/journal_watchdog_thisboot.txt" 2>&1 || true
  step "journal_watchdog_thisboot.txt: $(wc -l <"$out/journal_watchdog_thisboot.txt") lines"

  journalctl -b 0 -g 'suspend|resume|sleep|s2idle|systemd-sleep' --no-pager \
    >"$out/journal_sleep_events.txt" 2>&1
  step "journal_sleep_events.txt: $(wc -l <"$out/journal_sleep_events.txt") lines"

  if [ -r /sys/firmware/acpi/tables/DSDT ]; then
    cp /sys/firmware/acpi/tables/DSDT "$out/DSDT.aml" 2>&1
    step "DSDT.aml: $(stat -c%s "$out/DSDT.aml") bytes (decompile with: iasl -d $out/DSDT.aml)"
  fi

  hdr "PCIe AER counters (modem + parent)"
  for slot in 0000:71:00.0 0000:00:1c.0; do
    echo "--- $slot ---"
    # AER Correctable + Uncorrectable status registers in ECAP_AER+10.l/+0x14.l.
    if [ -r "/sys/bus/pci/devices/$slot/aer_dev_correctable" ]; then
      echo "correctable:"
      cat "/sys/bus/pci/devices/$slot/aer_dev_correctable" 2>&1
      echo "fatal:"
      cat "/sys/bus/pci/devices/$slot/aer_dev_fatal" 2>&1
      echo "nonfatal:"
      cat "/sys/bus/pci/devices/$slot/aer_dev_nonfatal" 2>&1
    else
      echo "(no AER sysfs counters for $slot)"
    fi
  done

  # PCI reset_method shows which reset hooks the kernel discovered. Presence
  # of `acpi` here means the DSDT's `_RST` method on \_SB.PC00.RP02.PXSX is
  # exposed — which on this platform requires BIOS variable WWEN >= 1. If
  # only `flr bus` appears, the WWAN slot power-cycle (FHRF/SHRF in DSDT)
  # is gated off and `pci_try_reset_function` in the kernel's recovery_work
  # can't reach the real platform reset. See modem_suspend_research.md
  # §DSDT decompile findings for the full mechanism.
  hdr "PCI reset_method (presence of 'acpi' indicates BIOS WWEN >= 1)"
  for slot in 0000:71:00.0 0000:00:1c.0; do
    echo -n "$slot: "
    cat "/sys/bus/pci/devices/$slot/reset_method" 2>&1
  done

  # Dell BIOS attributes via dell-wmi-sysman. WWAN-related entries; the
  # current_value is what (if anything) flips the DSDT-side WWEN variable
  # that gates the platform reset path. Capture every WWAN/sleep attr so
  # diffing across BIOS toggles is easy.
  hdr "Dell BIOS attributes (WWAN/sleep — root-only current_value)"
  local fwattr=/sys/class/firmware-attributes/dell-wmi-sysman/attributes
  if [ -d "$fwattr" ]; then
    for attr in $(ls "$fwattr" | grep -iE 'wwan|cellular|gnss|gps|sleep|standby|wake|s0|powerm|wireless'); do
      local cur poss disp
      cur=$(cat "$fwattr/$attr/current_value" 2>&1)
      poss=$(cat "$fwattr/$attr/possible_values" 2>/dev/null)
      disp=$(cat "$fwattr/$attr/display_name" 2>/dev/null)
      printf '  %-22s = %-14s  [%s]  %s\n' "$attr" "$cur" "${poss:-?}" "${disp:-?}"
    done
  else
    step "no dell-wmi-sysman — different platform or driver not loaded"
  fi

  # Make the snapshot readable to the unprivileged user that owns the repo.
  # Root-only outputs (DSDT.aml at mode 0400, etc.) otherwise block iasl.
  chmod -R u+rwX,go+rX "$out" 2>/dev/null || true
  chown -R --reference="$REPO" "$out" 2>/dev/null || true

  say "done — outputs at $out"
  ls -la "$out"
}

# ─────────────────────────── diagnose ──────────────────────────────────────
cmd_diagnose() {
  local kill_wifi=0
  for arg in "$@"; do
    case "$arg" in
      --kill-wifi) kill_wifi=1 ;;
      *) die "unknown arg: $arg" ;;
    esac
  done

  local ts out start_epoch start_human
  ts=$(date +%Y%m%d-%H%M%S)
  out="$REPO/debug/rugged-mobile-net-diag/$ts"
  mkdir -p "$out"
  exec > >(tee -a "$out/00-script.log") 2>&1
  start_epoch=$(date +%s)
  start_human=$(date '+%Y-%m-%d %H:%M:%S')

  # ── recovery trap: always tear down cellular; restore WiFi if we killed it
  recover() {
    local rc=$?
    say "RECOVERY (script exit=$rc)"
    nmcli connection down "$GSM_CONN" 2>&1 || true
    if [ "$kill_wifi" = 1 ]; then
      hdr "WiFi back on"
      nmcli radio wifi on 2>&1 || true
      for i in $(seq 1 45); do
        local s
        s=$(nmcli -t -f DEVICE,STATE device 2>/dev/null \
          | awk -F: -v d="$WIFI_DEV" '$1==d{print $2}')
        printf '  [%2ds] wifi=%s\n' "$i" "$s"
        [ "$s" = "connected" ] && break
        sleep 1
      done
    fi
    hdr "post-recovery routes"
    ip -4 route 2>&1 || true
    hdr "post-recovery devices"
    nmcli device status 2>&1 || true
    hdr "DONE"
    echo "Outputs: $out"
    exit "$rc"
  }
  trap recover EXIT

  # ── PHASE 1: pre-state
  say "PHASE 1 — pre-state ($start_human)"
  uname -a
  date
  hdr "ip addr"
  ip addr
  hdr "ip route v4"
  ip -4 route
  hdr "ip route v6"
  ip -6 route
  hdr "nmcli devices"
  nmcli device status
  hdr "rfkill"
  rfkill list
  hdr "mmcli -L"
  mmcli -L
  local mid
  mid=$(modem_id)
  echo "modem id: ${mid:-<none>}"
  if [ -n "$mid" ]; then
    hdr "mmcli -m $mid"
    mmcli -m "$mid" || true
    local sim
    sim=$(sim_id "$mid")
    [ -n "$sim" ] && {
      hdr "active SIM"
      mmcli -i "$sim" || true
    }
  fi
  hdr "mbim slots"
  mbimcli -d "$MBIM_DEV" -p --ms-query-device-slot-mappings 2>&1 || true
  for slot in 0 1; do
    mbimcli -d "$MBIM_DEV" -p --ms-query-slot-info-status="$slot" 2>&1 || true
  done

  # ── PHASE 2: ensure physical slot active (slot 0)
  say "PHASE 2 — confirm slot 0 (physical)"
  local mapping
  mapping=$(mbimcli -d "$MBIM_DEV" -p --ms-query-device-slot-mappings 2>&1 \
    | grep -oE "slot '[0-9]'" | head -1 | tr -d "'" | awk '{print $2}')
  if [ "$mapping" != "0" ]; then
    step "active slot is $mapping; switching to 0"
    cmd_slot 0
  else
    step "slot 0 already active."
  fi

  # ── PHASE 3: optional WiFi off
  if [ "$kill_wifi" = 1 ]; then
    say "PHASE 3 — turn WiFi off (KILL_WIFI)"
    nmcli radio wifi off
    sleep 4
    nmcli -t -f DEVICE,STATE,CONNECTION device | grep -E "$WIFI_DEV|wifi"
    rfkill list
    ip -4 route show default
  else
    say "PHASE 3 — skipped (WiFi stays up; cellular tests bind to wwan0)"
  fi

  # ── PHASE 4: bring modem online (rely on fcc-unlock.d + watchdog)
  say "PHASE 4 — bring modem online"
  hdr "wait up to 90s for registered/connected"
  wait_for_state 90 registered enabled connected connecting \
    || step "did not reach a usable state in 90s"
  mid=$(modem_id)
  hdr "lock to LTE"
  mmcli -m "$mid" --set-allowed-modes=4g 2>&1 || true
  sleep 3
  hdr "bring up '$GSM_CONN'"
  nmcli connection up "$GSM_CONN" 2>&1 || true
  sleep 5
  hdr "modem post-connect"
  mmcli -m "$mid" || true
  hdr "wwan0 addr"
  ip addr show wwan0 2>&1 || true
  hdr "v4 routes"
  ip -4 route

  # ── PHASE 5: diagnostics
  say "PHASE 5 — diagnostics (cellular bound)"
  hdr "signal (extended)"
  mmcli -m "$mid" --signal-setup=5 2>&1 || true
  sleep 6
  mmcli -m "$mid" --signal-get 2>&1 || true
  for q in --query-signal-state --query-register-state \
    --query-packet-service-state '--query-connection-state=0' \
    --query-subscriber-ready-status --query-home-provider; do
    hdr "mbim $q"
    eval mbimcli -d "$MBIM_DEV" -p $q 2>&1 || true
  done

  hdr "ICMP ping 8.8.8.8 (wwan0)"
  ping -I wwan0 -c 5 -W 3 8.8.8.8 2>&1 || true

  local wwan_ip
  wwan_ip=$(ip -4 -o addr show wwan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  echo "wwan0 source IP: ${wwan_ip:-<none>}"

  hdr "DNS lookup (bound to wwan0 src)"
  if [ -n "$wwan_ip" ]; then
    dig @8.8.8.8 -b "$wwan_ip" +tries=1 +time=3 google.com 2>&1 || true
  fi

  hdr "captive-portal probe (Android generate_204)"
  curl --interface wwan0 -4 -sv -o /dev/null \
    -w 'http=%{http_code} size=%{size_download}\n' \
    http://connectivitycheck.gstatic.com/generate_204 2>&1 \
    | grep -E '^http=|^\* (Connected|HTTP)' || true

  hdr "HTTP throughput — Tele2 1MB"
  curl --interface wwan0 -4 -o /dev/null -sm 30 \
    -w "speed: %{speed_download} B/s  time: %{time_total}s  http: %{http_code}\n" \
    http://speedtest.tele2.net/1MB.zip 2>&1 || true

  hdr "HTTPS throughput — Tele2 1MB (with -v on failure)"
  curl --interface wwan0 -4 -o /dev/null -sm 30 \
    -w "speed: %{speed_download} B/s  time: %{time_total}s  http: %{http_code}\n" \
    https://speedtest.tele2.net/1MB.zip 2>&1 || true
  hdr "HTTPS verbose probe (8s)"
  curl --interface wwan0 -4 -v -o /dev/null -sm 8 \
    https://speedtest.tele2.net/1MB.zip 2>&1 \
    | grep -E '^(\*|< HTTP)' | head -25 || true

  hdr "SMOKING GUN — ICMP RTT vs TCP socket stats during 10MB transfer"
  (curl --interface wwan0 -4 -o /dev/null -sm 25 \
    http://speedtest.tele2.net/10MB.zip >/dev/null 2>&1) &
  local cpid=$!
  sleep 3
  echo "--- ss src $wwan_ip (cellular sockets only) ---"
  [ -n "$wwan_ip" ] && ss -tnei src "$wwan_ip" 2>&1 || echo "no wwan0 IP"
  echo "--- ICMP ping (5x at 0.5s) ---"
  ping -I wwan0 -c 5 -W 3 -i 0.5 8.8.8.8 2>&1 || true
  wait $cpid 2>/dev/null || true

  hdr "Final 10MB throughput"
  curl --interface wwan0 -4 -o /dev/null -sm 60 \
    -w "speed: %{speed_download} B/s  time: %{time_total}s  http: %{http_code}\n" \
    http://speedtest.tele2.net/10MB.zip 2>&1 || true

  hdr "Path MTU sweep (DF-bit)"
  for size in 1172 1200 1228 1252 1272 1372 1472; do
    echo "--- payload=$size (IP=$((size + 28))) ---"
    ping -I wwan0 -M do -c 2 -W 2 -s "$size" 8.8.8.8 2>&1 \
      | grep -E 'transmitted|too long' | head -1
  done

  # ── PHASE 6: collect logs
  say "PHASE 6 — collect logs"
  journalctl -u ModemManager --since "@$start_epoch" --no-pager >"$out/01-mm.log" 2>&1
  journalctl -u NetworkManager --since "@$start_epoch" --no-pager >"$out/02-nm.log" 2>&1
  journalctl -u foxflss-watchdog --since "@$start_epoch" --no-pager >"$out/03-watchdog.log" 2>&1 || true
  journalctl -k --since "@$start_epoch" --no-pager >"$out/04-kernel.log" 2>&1
  journalctl --since "@$start_epoch" --no-pager >"$out/05-journal-all.log" 2>&1
  mmcli -L >"$out/10-mmcli-list.txt" 2>&1
  mmcli -m "$mid" >"$out/11-mmcli-modem.txt" 2>&1 || true
  local sim
  sim=$(sim_id "$mid")
  [ -n "$sim" ] && mmcli -i "$sim" >"$out/12-mmcli-sim.txt" 2>&1 || true
  {
    for q in --query-device-caps --ms-query-device-slot-mappings \
      '--ms-query-slot-info-status=0' '--ms-query-slot-info-status=1' \
      --query-signal-state --query-register-state \
      --query-packet-service-state '--query-connection-state=0' \
      --query-subscriber-ready-status --query-home-provider \
      --query-radio-state --query-pin-state; do
      echo "=== mbimcli $q ==="
      eval mbimcli -d "$MBIM_DEV" -p $q 2>&1
      echo
    done
  } >"$out/20-mbim.txt" 2>&1
  {
    echo "=== ip addr ==="
    ip addr
    echo
    echo "=== ip -4 route ==="
    ip -4 route
    echo
    echo "=== ip -6 route ==="
    ip -6 route
    echo
    echo "=== ip rule ==="
    ip rule
  } >"$out/30-ip.txt" 2>&1
  {
    echo "=== nmcli device status ==="
    nmcli device status
    echo
    echo "=== nmcli connection show ==="
    nmcli connection show
    echo
    echo "=== nmcli connection show '$GSM_CONN' ==="
    nmcli connection show "$GSM_CONN"
  } >"$out/40-nmcli.txt" 2>&1

  say "PHASE 7 — handing off to recovery trap"
  ls -la "$out"
}

# ─────────────────────────── dispatch ──────────────────────────────────────
sub="${1:-}"
shift || true
case "$sub" in
  '' | -h | --help)
    sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
    ;;
  status)
    require_root
    cmd_status
    ;;
  diagnose)
    require_root
    cmd_diagnose "$@"
    ;;
  slot)
    require_root
    cmd_slot "$@"
    ;;
  esim)
    require_root
    cmd_esim "$@"
    ;;
  unlock)
    require_root
    cmd_unlock
    ;;
  try-5g)
    require_root
    cmd_try_5g
    ;;
  recover)
    require_root
    cmd_recover
    ;;
  dump)
    require_root
    cmd_dump
    ;;
  *) die "unknown subcommand: $sub  (try $0 --help)" ;;
esac
