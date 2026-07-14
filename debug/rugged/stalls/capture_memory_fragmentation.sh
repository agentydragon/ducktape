#!/usr/bin/env bash
# Capture the caller that wakes kswapd for high-order allocation/fragmentation.
# Run as: sudo -E debug/rugged/stalls/capture_memory_fragmentation.sh --duration 60
set -euo pipefail

duration=30
out_dir=""
call_graph=0
watch_seconds=0
trigger_pswpout_pages=4096
trigger_compact_stalls=10
shopt -s nullglob

usage() {
  cat >&2 <<'EOF'
Usage: sudo -E capture_memory_fragmentation.sh [options]

Records low-rate kswapd and compaction wakeups plus before/after memory state.
No sysctl, swap, or process state is changed. Output defaults to /tmp.

Options:
  --duration SECONDS             perf capture duration, default: 30
  --out DIR                      capture destination, default: /tmp/...
  --call-graph                   include allocation call stacks
  --watch SECONDS                poll until a swap/compaction spike, then capture
  --trigger-pswpout-pages PAGES  per-second swapout trigger, default: 4096 (16 MiB)
  --trigger-compact-stalls N     per-second compaction-stall trigger, default: 10

--call-graph adds kernel call stacks to compaction events.  Use a short capture
(for example, 20 seconds); output is larger but attributes allocations to the
relevant graphics or memory-management code path.
EOF
}

while (($#)); do
  case "$1" in
    --duration)
      duration="$2"
      shift 2
      ;;
    --out)
      out_dir="$2"
      shift 2
      ;;
    --call-graph)
      call_graph=1
      shift
      ;;
    --watch)
      watch_seconds="$2"
      shift 2
      ;;
    --trigger-pswpout-pages)
      trigger_pswpout_pages="$2"
      shift 2
      ;;
    --trigger-compact-stalls)
      trigger_compact_stalls="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this through sudo so perf can access kernel tracepoints." >&2
  exit 1
fi

for bin in awk cat date free perf swapon sysctl; do
  command -v "$bin" >/dev/null || {
    echo "Missing required command: $bin" >&2
    exit 1
  }
done

for value in "$duration" "$watch_seconds" "$trigger_pswpout_pages" "$trigger_compact_stalls"; do
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "numeric options must be non-negative integers" >&2
    exit 2
  fi
done
if ((duration == 0)); then
  echo "--duration must be positive" >&2
  exit 2
fi

if [[ -z "$out_dir" ]]; then
  out_dir="/tmp/rugged-memory-fragmentation-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$out_dir"

snapshot() {
  local prefix="$1"
  {
    date --iso-8601=seconds
    free -h
    swapon --show --bytes
    cat /proc/pressure/memory
    cat /proc/buddyinfo
    cat /proc/pagetypeinfo
    cat /proc/zoneinfo
    egrep 'pswpin|pswpout|pgscan_kswapd|pgsteal_kswapd|pgscan_direct|pgsteal_direct|compact_|kswapd_' /proc/vmstat
    sysctl vm.swappiness vm.compaction_proactiveness vm.extfrag_threshold
    cat /sys/kernel/mm/transparent_hugepage/enabled
    cat /sys/kernel/mm/transparent_hugepage/defrag
  } >"$out_dir/${prefix}-state.txt"
}

snapshot_gpu_debugfs() {
  local prefix="$1"
  {
    date --iso-8601=seconds
    if command -v lspci >/dev/null; then
      lspci -nnk
    fi
    for file in /sys/kernel/debug/dri/*/name \
      /sys/kernel/debug/dri/*/clients \
      /sys/kernel/debug/dri/*/gem_objects \
      /sys/kernel/debug/dri/*/ttm_page_pool; do
      [[ -r "$file" ]] || continue
      printf '\n### %s\n' "$file"
      cat "$file"
    done
  } >"$out_dir/${prefix}-gpu-debugfs.txt"
}

snapshot before
snapshot_gpu_debugfs before

vmstat_value() {
  awk -v wanted="$1" '$1 == wanted { print $2; exit }' /proc/vmstat
}

if ((watch_seconds)); then
  watch_log="$out_dir/watch.tsv"
  printf 'timestamp\tpswpout_delta_pages\tcompact_stall_delta\n' >"$watch_log"
  previous_pswpout="$(vmstat_value pswpout)"
  previous_stalls="$(vmstat_value compact_stall)"
  deadline=$((SECONDS + watch_seconds))
  printf 'Watching up to %ss for swapout >= %s pages/s and compaction stalls >= %s/s\n' \
    "$watch_seconds" "$trigger_pswpout_pages" "$trigger_compact_stalls"
  while ((SECONDS < deadline)); do
    sleep 1
    current_pswpout="$(vmstat_value pswpout)"
    current_stalls="$(vmstat_value compact_stall)"
    delta_pswpout=$((current_pswpout - previous_pswpout))
    delta_stalls=$((current_stalls - previous_stalls))
    printf '%s\t%s\t%s\n' "$(date --iso-8601=seconds)" "$delta_pswpout" "$delta_stalls" >>"$watch_log"
    previous_pswpout="$current_pswpout"
    previous_stalls="$current_stalls"
    if ((delta_pswpout >= trigger_pswpout_pages && delta_stalls >= trigger_compact_stalls)); then
      printf 'Trigger reached: swapout=%s pages/s, compaction_stalls=%s/s\n' \
        "$delta_pswpout" "$delta_stalls" | tee "$out_dir/trigger.txt"
      call_graph=1
      break
    fi
  done
  if [[ ! -f "$out_dir/trigger.txt" ]]; then
    echo 'No trigger observed before watch timeout.' | tee "$out_dir/trigger.txt"
    if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
      chown -R "$SUDO_UID:$SUDO_GID" "$out_dir"
    fi
    exit 0
  fi
fi

tracefs=/sys/kernel/tracing
events=()
for event in \
  vmscan:mm_vmscan_kswapd_wake \
  compaction:mm_compaction_kcompactd_wake \
  compaction:mm_compaction_begin \
  compaction:mm_compaction_end; do
  group=${event%%:*}
  name=${event#*:}
  if [[ -e "$tracefs/events/$group/$name/enable" ]]; then
    events+=("-e" "$event")
  else
    printf 'missing tracepoint: %s\n' "$event" >>"$out_dir/missing-tracepoints.txt"
  fi
done

if ((${#events[@]} == 0)); then
  echo "No requested tracepoints are available; see $out_dir/missing-tracepoints.txt" >&2
  exit 1
fi

printf 'Recording %ss into %s\n' "$duration" "$out_dir"
perf_args=(record -a -o "$out_dir/perf.data")
if ((call_graph)); then
  perf_args+=(-g)
fi
perf_args+=("${events[@]}" -- sleep "$duration")
perf "${perf_args[@]}"
perf script -i "$out_dir/perf.data" >"$out_dir/perf-script.txt"
awk '
  /compaction:mm_compaction_begin/ { print $1, $2 }
' "$out_dir/perf-script.txt" | sort | uniq -c | sort -nr >"$out_dir/direct-compaction-callers.txt"

snapshot after
snapshot_gpu_debugfs after

if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  chown -R "$SUDO_UID:$SUDO_GID" "$out_dir"
fi

printf 'Capture complete: %s\n' "$out_dir"
printf 'Direct compaction callers:\n'
cat "$out_dir/direct-compaction-callers.txt"
