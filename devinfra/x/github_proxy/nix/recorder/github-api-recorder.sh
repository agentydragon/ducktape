#!/usr/bin/env bash
# Runs the connection probe and records every outbound TCP connection.
#
# Nothing is filtered. An earlier version dropped private destinations to cut
# kubelet-probe noise (~10/s on this node), and every analysis on top of it then
# filtered again to a hand-picked list of GitHub API addresses. That second filter
# was wrong -- GitHub's /meta lists 140.82.116.4 and 20.29.134.0/24 under `api`,
# neither of which was matched -- and it blinded the analysis for hours while
# reading as "nothing else touches the API". Filtering at capture time makes that
# class of mistake unrecoverable, because the data to correct it was never kept.
# Volume is roughly 10 records/s, ~90MB/day, which logrotate handles.
#
# Filter at analysis time, from api.github.com/meta, never here.
set -uo pipefail

exec bpftrace -B line "$1"
