#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: debug/rugged/stalls/probe_gnome_stalls.sh [options]

Probe GNOME/session responsiveness while looking for hard desktop stalls.
The output is a TSV with GNOME Shell DBus latency, session bus latency, PSI,
load, and top CPU consumers. Process command lines are not logged.

Options:
  --duration SECONDS   Capture duration, default: 1800
  --interval SECONDS   Sleep between samples, default: 0.2
  --timeout SECONDS    Per-DBus-call timeout, default: 5
  --slow-ms MS         Trigger snapshot threshold, default: 500
  --snapshot-cooldown SECONDS
                       Minimum gap between snapshots, default: 5
  --journal-window SECONDS
                       Journal lookback for snapshots, default: 90
  --mark-file PATH     Manual marker file. Appending a line triggers a snapshot.
                       Default: <out-dir>/manual_marks.tsv
  --attach-stacks      Attach gdb to GNOME Shell for user-space backtraces.
                       This pauses GNOME Shell briefly; default is on.
  --no-attach-stacks   Disable gdb stack attachment.
  --gcore              Generate a live GNOME Shell core with gcore in snapshots.
                       Very intrusive and may write a large sensitive file.
                       Default is on.
  --no-gcore           Disable live core capture.
  --perf-seconds SECONDS
                       Run perf against GNOME Shell in snapshots, default: 10
  --out PATH           Output TSV path
  --out-dir DIR        Output directory, default: debug/rugged/stalls/captures
  --no-snapshots       Do not capture trigger snapshots
  -h, --help           Show this help
EOF
}

duration=1800
interval=0.2
call_timeout=5
slow_ms=500
snapshot_cooldown=5
journal_window=90
snapshots=1
attach_stacks=1
gcore=1
perf_seconds=10
out_dir="debug/rugged/stalls/captures"
out=""
mark_file=""

while (($# > 0)); do
  case "$1" in
    --duration)
      duration="$2"
      shift 2
      ;;
    --interval)
      interval="$2"
      shift 2
      ;;
    --timeout)
      call_timeout="$2"
      shift 2
      ;;
    --slow-ms)
      slow_ms="$2"
      shift 2
      ;;
    --snapshot-cooldown)
      snapshot_cooldown="$2"
      shift 2
      ;;
    --journal-window)
      journal_window="$2"
      shift 2
      ;;
    --mark-file)
      mark_file="$2"
      shift 2
      ;;
    --attach-stacks)
      attach_stacks=1
      shift
      ;;
    --no-attach-stacks)
      attach_stacks=0
      shift
      ;;
    --gcore)
      gcore=1
      shift
      ;;
    --no-gcore)
      gcore=0
      shift
      ;;
    --perf-seconds)
      perf_seconds="$2"
      shift 2
      ;;
    --out)
      out="$2"
      shift 2
      ;;
    --out-dir)
      out_dir="$2"
      shift 2
      ;;
    --no-snapshots)
      snapshots=0
      shift
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

for bin in awk cat date gdbus head journalctl mkdir mktemp ps sed sleep timeout; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required command: $bin" >&2
    exit 1
  fi
done

target_uid=""
target_gid=""
target_user=""
target_home=""
session_uid="$(id -u)"
if [[ "${EUID:-$(id -u)}" == 0 && -n "${SUDO_UID:-}" ]]; then
  target_uid="$SUDO_UID"
  target_gid="${SUDO_GID:-}"
  target_user="${SUDO_USER:-}"
  session_uid="$target_uid"
  if [[ -z "$target_user" ]]; then
    target_user="$(getent passwd "$target_uid" 2>/dev/null | awk -F: '{ print $1 }')"
  fi
  target_home="$(getent passwd "$target_uid" 2>/dev/null | awk -F: '{ print $6 }')"

  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$target_uid" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$target_uid"
  fi
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$target_uid/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$target_uid/bus"
  fi
fi

if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  echo "DBUS_SESSION_BUS_ADDRESS is not set; run this from the desktop session." >&2
  echo "If running with sudo, preserve the user bus or rely on SUDO_UID:" >&2
  echo "  sudo -E debug/rugged/stalls/probe_gnome_stalls.sh ..." >&2
  exit 1
fi

session_prefix=()
if [[ -n "$target_user" ]] && command -v runuser >/dev/null 2>&1; then
  session_prefix=(
    runuser -u "$target_user" --
    env
    "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
    "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$target_uid}"
  )
  if [[ -n "$target_home" ]]; then
    session_prefix+=("HOME=$target_home")
  fi
fi

if [[ -z "$out" ]]; then
  mkdir -p "$out_dir"
  out="$out_dir/$(date +%Y%m%d-%H%M%S).tsv"
else
  mkdir -p "$(dirname "$out")"
fi

if [[ -z "$mark_file" ]]; then
  mark_file="$out_dir/manual_marks.tsv"
fi
mkdir -p "$(dirname "$mark_file")"
touch "$mark_file"
if [[ -n "$target_uid" && -n "$target_gid" ]] && command -v chown >/dev/null 2>&1; then
  chown "$target_uid:$target_gid" "$out_dir" "$mark_file" 2>/dev/null || true
fi

snapshot_dir="$out_dir/snapshots"
if ((snapshots)); then
  mkdir -p "$snapshot_dir"
fi

fix_owner() {
  if [[ -n "$target_uid" && -n "$target_gid" ]] && command -v chown >/dev/null 2>&1; then
    chown -R "$target_uid:$target_gid" "$@" 2>/dev/null || true
  fi
}

pressure_avg10() {
  local file="$1"
  local line="$2"
  awk -v wanted_line="$line" '
    NR == wanted_line {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^avg10=/) {
          sub(/^avg10=/, "", $i)
          print $i
          exit
        }
      }
    }
  ' "$file"
}

top_processes() {
  ps -eo comm=,%cpu= --sort=-%cpu \
    | awk '
      $1 ~ /^(ps|awk|sort|head|timeout|gdbus|date|sleep)$/ { next }
      shown < 8 {
        gsub(/[\t ,]+/, "_", $1)
        printf "%s:%s,", $1, $2
        shown++
      }
    '
}

gnome_shell_pid() {
  "${session_prefix[@]}" gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.GetConnectionUnixProcessID org.gnome.Shell \
    2>/dev/null \
    | sed -n 's/^(uint32 \([0-9][0-9]*\),)$/\1/p'
}

measure_call() {
  local label="$1"
  local __ms_var="$2"
  local __ok_var="$3"
  shift 3

  local start_ms end_ms ok status_file worker_pid status elapsed_ms
  start_ms="$(date +%s%3N)"
  status_file="$(mktemp)"

  (
    set +e
    timeout "$call_timeout" "$@" >/dev/null 2>&1
    code="$?"
    printf '%s\n' "$code" >"$status_file"
  ) &
  worker_pid="$!"

  while [[ ! -s "$status_file" ]]; do
    elapsed_ms="$(($(date +%s%3N) - start_ms))"
    if ((elapsed_ms >= slow_ms)); then
      if ((snapshots)) && (($(date +%s) - last_snapshot_s >= snapshot_cooldown)); then
        local cpu_some10 io_full10 mem_full10 load1 top
        cpu_some10="$(pressure_avg10 /proc/pressure/cpu 1)"
        io_full10="$(pressure_avg10 /proc/pressure/io 2)"
        mem_full10="$(pressure_avg10 /proc/pressure/memory 2)"
        load1="$(awk '{ print $1 }' /proc/loadavg)"
        top="$(top_processes)"
        capture_snapshot "$start_ms" "$elapsed_ms" "pending" "" "" \
          "$cpu_some10" "$io_full10" "$mem_full10" "$load1" "$top" "$label" &
        snapshot_pids+=("$!")
        last_snapshot_s="$(date +%s)"
      fi
      break
    fi
    sleep 0.02
  done

  while [[ ! -s "$status_file" ]]; do
    sleep 0.02
  done
  wait "$worker_pid" || true

  status="$(cat "$status_file" 2>/dev/null || echo 124)"
  rm -f "$status_file"

  if [[ "$status" == 0 ]]; then
    ok=1
  else
    ok=0
  fi
  end_ms="$(date +%s%3N)"

  printf -v "$__ms_var" '%s' "$((end_ms - start_ms))"
  printf -v "$__ok_var" '%s' "$ok"
}

copy_if_readable() {
  local src="$1"
  local dst="$2"

  if [[ -r "$src" ]]; then
    cat "$src" >"$dst" 2>&1 || true
  fi
}

capture_command() {
  local dst="$1"
  shift

  if command -v "$1" >/dev/null 2>&1; then
    "$@" >"$dst" 2>&1 || true
  fi
}

capture_gnome_context() {
  local dir="$1"

  {
    echo "=== host ==="
    hostname
    uname -a
    date --iso-8601=ns
    echo "=== gnome-shell ==="
    if command -v gnome-shell >/dev/null 2>&1; then
      gnome-shell --version
    fi
    echo "=== mutter experimental features ==="
    if command -v gsettings >/dev/null 2>&1; then
      "${session_prefix[@]}" gsettings get org.gnome.mutter experimental-features
      "${session_prefix[@]}" gsettings get org.gnome.shell disable-user-extensions
      "${session_prefix[@]}" gsettings get org.gnome.shell enabled-extensions
    fi
    echo "=== enabled extensions ==="
    if command -v gnome-extensions >/dev/null 2>&1; then
      "${session_prefix[@]}" gnome-extensions list --enabled
    fi
  } >"$dir/gnome-session-context.txt" 2>&1 || true

  capture_command "$dir/session-bus-list.txt" "${session_prefix[@]}" busctl --user list
  capture_command "$dir/system-bus-list.txt" busctl list
}

capture_drm_sysfs() {
  local dir="$1"

  {
    for card in /sys/class/drm/card*; do
      [[ -d "$card/device" ]] || continue
      echo "=== $card ==="
      readlink -f "$card" || true
      if [[ -e "$card/device/driver" ]]; then
        printf 'driver='
        readlink -f "$card/device/driver" || true
      fi
      for file in \
        "$card/device/vendor" \
        "$card/device/device" \
        "$card/device/subsystem_vendor" \
        "$card/device/subsystem_device" \
        "$card/device/uevent" \
        "$card/device/power/runtime_status" \
        "$card/device/power/runtime_suspended_time" \
        "$card/device/power/runtime_active_time" \
        "$card/device/gpu_busy_percent"; do
        if [[ -r "$file" ]]; then
          echo "--- $file"
          cat "$file" 2>&1 || true
        fi
      done
    done
  } >"$dir/drm-sysfs.txt" 2>&1 || true
}

capture_snapshot() {
  local ts_ms="$1"
  local shell_ms="$2"
  local shell_ok="$3"
  local bus_ms="$4"
  local bus_ok="$5"
  local cpu_some10="$6"
  local io_full10="$7"
  local mem_full10="$8"
  local load1="$9"
  local top="${10}"
  local trigger_label="${11:-unknown}"

  local dir pid since
  dir="$snapshot_dir/$ts_ms"
  mkdir -p "$dir"

  {
    printf 'ts_ms=%s\n' "$ts_ms"
    printf 'trigger=%s\n' "$trigger_label"
    printf 'captured_at=%s\n' "$(date --iso-8601=ns)"
    printf 'shell_ms=%s\nshell_ok=%s\nbus_ms=%s\nbus_ok=%s\n' \
      "$shell_ms" "$shell_ok" "$bus_ms" "$bus_ok"
    printf 'cpu_some10=%s\nio_full10=%s\nmem_full10=%s\nload1=%s\n' \
      "$cpu_some10" "$io_full10" "$mem_full10" "$load1"
    printf 'top=%s\n' "$top"
  } >"$dir/trigger.txt"

  {
    echo "=== cpu ==="
    cat /proc/pressure/cpu
    echo "=== io ==="
    cat /proc/pressure/io
    echo "=== memory ==="
    cat /proc/pressure/memory
  } >"$dir/pressure.txt" 2>&1 || true

  {
    echo "=== loadavg ==="
    cat /proc/loadavg
    echo "=== meminfo excerpt ==="
    awk '/^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapCached|SwapTotal|SwapFree|Dirty|Writeback):/ { print }' /proc/meminfo
    echo "=== vmstat ==="
    cat /proc/vmstat
  } >"$dir/system-proc.txt" 2>&1 || true

  capture_gnome_context "$dir"
  capture_drm_sysfs "$dir"

  ps -eo pid,ppid,stat,pri,ni,comm,%cpu,%mem,rss,vsz,wchan:32 --sort=-%cpu \
    >"$dir/processes-by-cpu.txt" 2>&1 || true
  ps -eo pid,ppid,stat,pri,ni,comm,%cpu,%mem,rss,vsz,wchan:32 --sort=-rss \
    | head -80 >"$dir/processes-by-rss.txt" 2>&1 || true
  ps -eLo pid,tid,psr,pri,ni,stat,pcpu,pmem,comm,wchan:32 --sort=-pcpu \
    | head -120 >"$dir/threads-by-cpu.txt" 2>&1 || true

  if command -v vmstat >/dev/null 2>&1; then
    vmstat 1 3 >"$dir/vmstat-1s.txt" 2>&1 || true
  fi
  if command -v iostat >/dev/null 2>&1; then
    iostat -xz 1 3 >"$dir/iostat-1s.txt" 2>&1 || true
  fi
  if command -v pidstat >/dev/null 2>&1; then
    pidstat -rudw 1 3 >"$dir/pidstat-1s.txt" 2>&1 || true
  fi

  pid="$(gnome_shell_pid || true)"
  if [[ -n "$pid" && -d "/proc/$pid" ]]; then
    printf '%s\n' "$pid" >"$dir/gnome-shell.pid"
    copy_if_readable "/proc/$pid/status" "$dir/gnome-shell.status"
    copy_if_readable "/proc/$pid/stat" "$dir/gnome-shell.stat"
    copy_if_readable "/proc/$pid/sched" "$dir/gnome-shell.sched"
    copy_if_readable "/proc/$pid/schedstat" "$dir/gnome-shell.schedstat"
    copy_if_readable "/proc/$pid/io" "$dir/gnome-shell.io"
    copy_if_readable "/proc/$pid/smaps_rollup" "$dir/gnome-shell.smaps_rollup"
    copy_if_readable "/proc/$pid/wchan" "$dir/gnome-shell.wchan"
    copy_if_readable "/proc/$pid/stack" "$dir/gnome-shell.kernel_stack"

    {
      printf 'tid\tcomm\tstate\twchan\tutime\tstime\tnice\tnum_threads\n'
      for task in "/proc/$pid/task/"*; do
        [[ -d "$task" ]] || continue
        local tid comm wchan stat state utime stime nice threads
        tid="${task##*/}"
        comm="$(cat "$task/comm" 2>/dev/null || true)"
        wchan="$(cat "$task/wchan" 2>/dev/null || true)"
        stat="$(cat "$task/stat" 2>/dev/null || true)"
        state="$(awk '{ print $3 }' <<<"$stat")"
        utime="$(awk '{ print $14 }' <<<"$stat")"
        stime="$(awk '{ print $15 }' <<<"$stat")"
        nice="$(awk '{ print $19 }' <<<"$stat")"
        threads="$(awk '{ print $20 }' <<<"$stat")"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$tid" "$comm" "$state" "$wchan" "$utime" "$stime" "$nice" "$threads"
      done
    } >"$dir/gnome-shell-threads.tsv" 2>&1 || true

    if command -v pidstat >/dev/null 2>&1; then
      pidstat -t -rudw -p "$pid" 1 3 >"$dir/gnome-shell-pidstat-1s.txt" 2>&1 || true
    fi

    if ((attach_stacks)) && command -v gdb >/dev/null 2>&1; then
      timeout 8s gdb -batch -p "$pid" \
        -ex 'set pagination off' \
        -ex 'info threads' \
        -ex 'thread apply all bt' \
        >"$dir/gnome-shell-gdb-bt.txt" 2>&1 || true
    fi

    if ((gcore)) && command -v gcore >/dev/null 2>&1; then
      local core_prefix core_file exe
      core_prefix="$dir/gnome-shell.core"
      timeout 45s gcore -o "$core_prefix" "$pid" \
        >"$dir/gnome-shell-gcore.txt" 2>&1 || true
      core_file="$(ls "$core_prefix".* 2>/dev/null | head -1 || true)"
      exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
      if [[ -n "$core_file" && -s "$core_file" && -n "$exe" && -x "$exe" ]] \
        && command -v gdb >/dev/null 2>&1; then
        timeout 45s gdb -batch "$exe" "$core_file" \
          -ex 'set pagination off' \
          -ex 'info threads' \
          -ex 'thread apply all bt' \
          >"$dir/gnome-shell-core-bt.txt" 2>&1 || true
      fi
    fi

    if ((perf_seconds > 0)) && command -v perf >/dev/null 2>&1; then
      timeout "$((perf_seconds + 5))s" perf record -F 99 -g -p "$pid" \
        -o "$dir/gnome-shell.perf.data" -- sleep "$perf_seconds" \
        >"$dir/gnome-shell-perf-record.txt" 2>&1 || true
      if [[ -s "$dir/gnome-shell.perf.data" ]]; then
        perf script -i "$dir/gnome-shell.perf.data" \
          >"$dir/gnome-shell-perf-script.txt" 2>&1 || true
      fi
    fi

    since="$(date --date="@$(($(date +%s) - journal_window))" '+%F %T')"
    journalctl _PID="$pid" --since "$since" --no-pager -o short-iso \
      >"$dir/journal-gnome-shell.txt" 2>&1 || true
  fi

  since="$(date --date="@$(($(date +%s) - journal_window))" '+%F %T')"
  journalctl -k --since "$since" --no-pager -o short-iso \
    >"$dir/journal-kernel.txt" 2>&1 || true
  journalctl -k --since "$since" --no-pager -o short-iso \
    | grep -Ei 'drm|xe|i915|gpu|hang|reset|fence|sched|rcu|hung|blocked|workqueue|nvme|thermal|thrott|acpi|firmware' \
      >"$dir/journal-kernel-graphics-and-stalls.txt" 2>&1 || true
  journalctl _UID="$session_uid" --since "$since" -p info..alert --no-pager -o short-iso \
    >"$dir/journal-user-info-plus.txt" 2>&1 || true
  journalctl --since "$since" -p warning..alert --no-pager -o short-iso \
    >"$dir/journal-system-warnings.txt" 2>&1 || true

  fix_owner "$dir"
}

capture_manual_marker_if_needed() {
  local line_count now_s cpu_some10 io_full10 mem_full10 load1 top ts_ms

  line_count="$(wc -l <"$mark_file" 2>/dev/null || echo 0)"
  if ((line_count <= mark_seen_lines)); then
    return
  fi
  mark_seen_lines="$line_count"

  if ((!snapshots)); then
    return
  fi

  now_s="$(date +%s)"
  if ((now_s - last_snapshot_s < snapshot_cooldown)); then
    return
  fi

  ts_ms="$(date +%s%3N)"
  cpu_some10="$(pressure_avg10 /proc/pressure/cpu 1)"
  io_full10="$(pressure_avg10 /proc/pressure/io 2)"
  mem_full10="$(pressure_avg10 /proc/pressure/memory 2)"
  load1="$(awk '{ print $1 }' /proc/loadavg)"
  top="$(top_processes)"
  capture_snapshot "$ts_ms" "" "" "" "" \
    "$cpu_some10" "$io_full10" "$mem_full10" "$load1" "$top" "manual-mark" &
  snapshot_pids+=("$!")
  last_snapshot_s="$now_s"
}

echo "Writing $out" >&2
echo "Manual marker file: $mark_file" >&2
cat >&2 <<EOF
To mark a felt stall after it recovers:
  printf '%s\\t%s\\n' "\$(date +%s%3N)" "felt stall" >> '$mark_file'
EOF

last_snapshot_s=0
mark_seen_lines="$(wc -l <"$mark_file" 2>/dev/null || echo 0)"
snapshot_pids=()
{
  printf 'ts_ms\tshell_ms\tshell_ok\tbus_ms\tbus_ok\tcpu_some10\tio_full10\tmem_full10\tload1\ttop\n'

  end_epoch=$(($(date +%s) + duration))
  while (($(date +%s) < end_epoch)); do
    ts_ms="$(date +%s%3N)"

    measure_call shell shell_ms shell_ok \
      "${session_prefix[@]}" \
      gdbus call --session \
      --dest org.gnome.Shell \
      --object-path /org/gnome/Shell \
      --method org.freedesktop.DBus.Peer.Ping

    measure_call bus bus_ms bus_ok \
      "${session_prefix[@]}" \
      gdbus call --session \
      --dest org.freedesktop.DBus \
      --object-path /org/freedesktop/DBus \
      --method org.freedesktop.DBus.ListNames

    capture_manual_marker_if_needed

    cpu_some10="$(pressure_avg10 /proc/pressure/cpu 1)"
    io_full10="$(pressure_avg10 /proc/pressure/io 2)"
    mem_full10="$(pressure_avg10 /proc/pressure/memory 2)"
    load1="$(awk '{ print $1 }' /proc/loadavg)"
    top="$(top_processes)"

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ts_ms" "$shell_ms" "$shell_ok" "$bus_ms" "$bus_ok" \
      "$cpu_some10" "$io_full10" "$mem_full10" "$load1" "$top"

    sleep "$interval"
  done
} >"$out"

for pid in "${snapshot_pids[@]}"; do
  wait "$pid" || true
done

fix_owner "$out" "$mark_file" "$snapshot_dir"

echo "Done: $out" >&2
echo >&2
echo "Slow samples:" >&2
awk -F'\t' 'NR == 1 || $2 >= 500 || $4 >= 500 || $7 >= 1 || $8 >= 1 { print }' "$out" \
  | tail -60 >&2
echo >&2
awk -F'\t' '
  BEGIN {
    shell = 0
    bus = 0
    cpu = 0
    io = 0
    mem = 0
  }
  NR > 1 {
    if ($2 > shell) shell = $2
    if ($4 > bus) bus = $4
    if ($6 > cpu) cpu = $6
    if ($7 > io) io = $7
    if ($8 > mem) mem = $8
  }
  END {
    print "Maxes: shell_ms=" shell, "bus_ms=" bus, "cpu_some10=" cpu, "io_full10=" io, "mem_full10=" mem
  }
' "$out" >&2
