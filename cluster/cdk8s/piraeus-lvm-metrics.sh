#!/bin/sh
# Report thin-pool usage with ordinary `lvs`; do not use LVM mutation commands.
# `lvs --readonly` is intentionally omitted because it suppresses kernel status values.
set -u

vg=${LVM_VOLUME_GROUP:?LVM_VOLUME_GROUP must name the selected volume group}
pool=${LVM_THIN_POOL:?LVM_THIN_POOL must name the selected thin pool}
output=/textfiles/lvm_thin_pool.prom
tmp="${output}.tmp.$$"

write_failure() {
  {
    printf '%s\n' \
      '# HELP lvm_thin_pool_collector_success Whether both thin-pool percentages were collected.' \
      '# TYPE lvm_thin_pool_collector_success gauge' \
      "lvm_thin_pool_collector_success{vg=\"${vg}\",pool=\"${pool}\"} 0"
  } >"$tmp" || return 0
  /bin/mv -f "$tmp" "$output" || return 0
  return 0
}

collect_once() {
  raw="$(/usr/bin/timeout --kill-after=1s 5s /usr/sbin/lvs \
    --reportformat json --units p --nosuffix --noheadings \
    -o vg_name,lv_name,data_percent,metadata_percent "${vg}/${pool}" 2>/dev/null)" || {
    write_failure
    return 0
  }
  values="$(printf '%s' "$raw" | /usr/bin/jq -er --arg vg "$vg" --arg pool "$pool" '
    [ .report[]?.lv[]? | select(.vg_name == $vg and .lv_name == $pool) ] as $selected
    | if ($selected | length) == 1
         and $selected[0].data_percent != ""
         and $selected[0].metadata_percent != "" then
        [($selected[0].data_percent | tonumber), ($selected[0].metadata_percent | tonumber)] | @tsv
      else
        error("thin-pool percentage fields missing")
      end
  ' 2>/dev/null)" || {
    write_failure
    return 0
  }
  data_percent="$(printf '%s' "$values" | /usr/bin/cut -f 1)"
  metadata_percent="$(printf '%s' "$values" | /usr/bin/cut -f 2)"
  for percentage in "$data_percent" "$metadata_percent"; do
    case "$percentage" in
      '' | *[!0-9.]* | .* | *.)
        write_failure
        return 0
        ;;
    esac
    case "$percentage" in
      *.*.*)
        write_failure
        return 0
        ;;
    esac
  done

  now="$(/bin/date +%s 2>/dev/null)" || {
    write_failure
    return 0
  }
  case "$now" in
    '' | *[!0-9]*)
      write_failure
      return 0
      ;;
  esac

  {
    printf '%s\n' \
      '# HELP lvm_thin_pool_data_percent Thin-pool data usage as a percent.' \
      '# TYPE lvm_thin_pool_data_percent gauge' \
      '# HELP lvm_thin_pool_metadata_percent Thin-pool metadata usage as a percent.' \
      '# TYPE lvm_thin_pool_metadata_percent gauge' \
      '# HELP lvm_thin_pool_collector_last_success_timestamp_seconds Unix time of the last successful sample.' \
      '# TYPE lvm_thin_pool_collector_last_success_timestamp_seconds gauge' \
      '# HELP lvm_thin_pool_collector_success Whether both thin-pool percentages were collected.' \
      '# TYPE lvm_thin_pool_collector_success gauge' \
      "lvm_thin_pool_data_percent{vg=\"${vg}\",pool=\"${pool}\"} ${data_percent}" \
      "lvm_thin_pool_metadata_percent{vg=\"${vg}\",pool=\"${pool}\"} ${metadata_percent}" \
      "lvm_thin_pool_collector_last_success_timestamp_seconds{vg=\"${vg}\",pool=\"${pool}\"} ${now}" \
      "lvm_thin_pool_collector_success{vg=\"${vg}\",pool=\"${pool}\"} 1"
  } >"$tmp" || {
    write_failure
    return 0
  }
  /bin/mv -f "$tmp" "$output" || write_failure
  return 0
}

trap '/bin/rm -f "$tmp"' EXIT
trap 'exit 0' HUP INT TERM
while :; do
  collect_once
  /bin/sleep 15
done
