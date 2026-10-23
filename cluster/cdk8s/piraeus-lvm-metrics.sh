#!/bin/sh
# Report thin-pool usage with ordinary `lvs`; do not use LVM mutation commands.
# `lvs --readonly` is intentionally omitted because it suppresses kernel status values.
set -u

vg=${LVM_VOLUME_GROUP:?LVM_VOLUME_GROUP must name the selected volume group}
pool=${LVM_THIN_POOL:?LVM_THIN_POOL must name the selected thin pool}
storage_pool=${LVM_STORAGE_POOL:?LVM_STORAGE_POOL must name the LINSTOR storage pool}
node=${LVM_NODE:?LVM_NODE must name the Kubernetes storage node}
output=/textfiles/lvm_thin_pool.prom
tmp="${output}.tmp.$$"

case "$node" in
  '' | *[!a-zA-Z0-9.-]*)
    exit 1
    ;;
esac

write_failure() {
  {
    printf '%s\n' \
      '# HELP lvm_thin_pool_collector_success Whether both thin-pool percentages were collected.' \
      '# TYPE lvm_thin_pool_collector_success gauge' \
      "lvm_thin_pool_collector_success{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} 0"
  } >"$tmp" || return 0
  /bin/mv -f "$tmp" "$output" || return 0
  return 0
}

collect_once() {
  raw="$(/usr/bin/timeout --kill-after=1s 5s /usr/sbin/lvs \
    --reportformat json --units b --nosuffix --noheadings \
    -o vg_name,lv_name,lv_size,lv_metadata_size,data_percent,metadata_percent "${vg}/${pool}" 2>/dev/null)" || {
    write_failure
    return 0
  }
  values="$(printf '%s' "$raw" | /usr/bin/jq -er --arg vg "$vg" --arg pool "$pool" '
    [ .report[]?.lv[]? | select(.vg_name == $vg and .lv_name == $pool) ] as $selected
    | if ($selected | length) == 1
         and $selected[0].data_percent != ""
         and $selected[0].metadata_percent != ""
         and $selected[0].lv_size != ""
         and $selected[0].lv_metadata_size != "" then
        ($selected[0].lv_size | tonumber | round) as $data_size
        | ($selected[0].lv_metadata_size | tonumber | round) as $metadata_size
        | ($selected[0].data_percent | tonumber) as $data_percent
        | ($selected[0].metadata_percent | tonumber) as $metadata_percent
        | [
            $data_percent,
            $metadata_percent,
            $data_size,
            $metadata_size,
            ($data_size * $data_percent / 100 | floor),
            ($metadata_size * $metadata_percent / 100 | floor)
          ] | @tsv
      else
        error("thin-pool capacity or percentage fields missing")
      end
  ' 2>/dev/null)" || {
    write_failure
    return 0
  }
  data_percent="$(printf '%s' "$values" | /usr/bin/cut -f 1)"
  metadata_percent="$(printf '%s' "$values" | /usr/bin/cut -f 2)"
  data_size="$(printf '%s' "$values" | /usr/bin/cut -f 3)"
  metadata_size="$(printf '%s' "$values" | /usr/bin/cut -f 4)"
  data_used_estimate="$(printf '%s' "$values" | /usr/bin/cut -f 5)"
  metadata_used_estimate="$(printf '%s' "$values" | /usr/bin/cut -f 6)"
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
  for bytes in "$data_size" "$metadata_size" "$data_used_estimate" "$metadata_used_estimate"; do
    case "$bytes" in
      '' | *[!0-9]*)
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
      '# HELP lvm_thin_pool_data_size_bytes Total data capacity of the thin pool.' \
      '# TYPE lvm_thin_pool_data_size_bytes gauge' \
      '# HELP lvm_thin_pool_data_used_bytes_estimate Estimated thin-pool data use, derived from LVM data_percent rounded to two decimal places.' \
      '# TYPE lvm_thin_pool_data_used_bytes_estimate gauge' \
      '# HELP lvm_thin_pool_metadata_size_bytes Total metadata capacity of the thin pool.' \
      '# TYPE lvm_thin_pool_metadata_size_bytes gauge' \
      '# HELP lvm_thin_pool_metadata_used_bytes_estimate Estimated thin-pool metadata use, derived from LVM metadata_percent rounded to two decimal places.' \
      '# TYPE lvm_thin_pool_metadata_used_bytes_estimate gauge' \
      '# HELP lvm_thin_pool_collector_last_success_timestamp_seconds Unix time of the last successful sample.' \
      '# TYPE lvm_thin_pool_collector_last_success_timestamp_seconds gauge' \
      '# HELP lvm_thin_pool_collector_success Whether both thin-pool percentages were collected.' \
      '# TYPE lvm_thin_pool_collector_success gauge' \
      "lvm_thin_pool_data_percent{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${data_percent}" \
      "lvm_thin_pool_metadata_percent{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${metadata_percent}" \
      "lvm_thin_pool_data_size_bytes{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${data_size}" \
      "lvm_thin_pool_data_used_bytes_estimate{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${data_used_estimate}" \
      "lvm_thin_pool_metadata_size_bytes{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${metadata_size}" \
      "lvm_thin_pool_metadata_used_bytes_estimate{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${metadata_used_estimate}" \
      "lvm_thin_pool_collector_last_success_timestamp_seconds{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} ${now}" \
      "lvm_thin_pool_collector_success{node=\"${node}\",storage_pool=\"${storage_pool}\",driver=\"LVM_THIN\",backing_pool=\"${vg}/${pool}\",vg=\"${vg}\",pool=\"${pool}\"} 1"
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
