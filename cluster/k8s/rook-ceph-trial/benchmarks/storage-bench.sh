#!/bin/sh
set -eu

elapsed() {
  awk -v start="$1" -v end="$2" 'BEGIN { printf "%.2f", end-start }'
}

run_one() {
  target="$1"
  root="$2"
  round="$3"
  work="$root/round-$round"
  rm -rf "$work"
  mkdir -p "$work/fsync" "$work/repo"

  start="$(awk '{print $1}' /proc/uptime)"
  i=1
  while [ "$i" -le 200 ]; do
    dd if=/dev/zero of="$work/fsync/$i" bs=4096 count=1 conv=fsync status=none
    i=$((i + 1))
  done
  end="$(awk '{print $1}' /proc/uptime)"
  printf '%s,%s,fsync_200,%s\n' "$round" "$target" "$(elapsed "$start" "$end")"

  git -C "$work/repo" init --quiet
  git -C "$work/repo" config user.name 'Ceph benchmark'
  git -C "$work/repo" config user.email bench@invalid.example
  start="$(awk '{print $1}' /proc/uptime)"
  i=1
  while [ "$i" -le 50 ]; do
    printf 'commit %s\n' "$i" >"$work/repo/file-$i"
    git -C "$work/repo" add "file-$i"
    git -C "$work/repo" commit --quiet -m "commit $i"
    i=$((i + 1))
  done
  end="$(awk '{print $1}' /proc/uptime)"
  printf '%s,%s,git_commit_50,%s\n' "$round" "$target" "$(elapsed "$start" "$end")"

  start="$(awk '{print $1}' /proc/uptime)"
  git clone --quiet "$work/repo" "$work/clone"
  end="$(awk '{print $1}' /proc/uptime)"
  printf '%s,%s,git_clone,%s\n' "$round" "$target" "$(elapsed "$start" "$end")"
  rm -rf "$work"
}

printf 'round,target,operation,seconds\n'
round=1
while [ "$round" -le 5 ]; do
  case $((round % 2)) in
    1)
      run_one seaweedfs-ssd /mnt/seaweedfs "$round"
      run_one cephfs-ssd /mnt/cephfs-ssd "$round"
      ;;
    0)
      run_one cephfs-ssd /mnt/cephfs-ssd "$round"
      run_one seaweedfs-ssd /mnt/seaweedfs "$round"
      ;;
  esac
  round=$((round + 1))
done
