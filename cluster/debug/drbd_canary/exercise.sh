#!/bin/sh
# Writes only the explicitly named disposable canary volume. fio reports latency.
set -eu

exercise=${1:?usage: exercise.sh seed|verify|fsync|stream TARGET}
target=${2:?usage: exercise.sh seed|verify|fsync|stream TARGET}
case "$target" in
  /dev/canary | /dev/sparse | /data/payload.bin) ;;
  *)
    echo "refusing a target outside the disposable canary fixtures: $target" >&2
    exit 2
    ;;
esac

if [ "$target" = /dev/sparse ] && [ "$exercise" != seed ] && [ "$exercise" != verify ]; then
  echo "refusing non-probe exercise on the 50Gi sparse volume: $exercise" >&2
  exit 2
fi

case "$exercise" in
  seed)
    set -- --rw=write --do_verify=1 --end_fsync=1
    ;;
  verify)
    set -- --rw=read --verify_only=1 --verify_state_load=0
    ;;
  fsync)
    set -- --rw=randwrite --fsync=1 --time_based=1 --runtime=20 --do_verify=0
    ;;
  stream)
    set -- --rw=randwrite --fsync=1 --time_based=1 --runtime=300 --do_verify=0 --status-interval=5
    ;;
  *)
    echo "unknown exercise: $exercise" >&2
    exit 2
    ;;
esac

exec fio --name=canary --filename="$target" --ioengine=psync --iodepth=1 \
  --direct=1 --bs=4k --size=64m --verify=crc32c --verify_fatal=1 \
  --output-format=json "$@"
