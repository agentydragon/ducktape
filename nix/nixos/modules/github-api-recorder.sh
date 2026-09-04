#!/usr/bin/env bash
# Runs the connection probe and drops records for destinations inside the machine
# or the cluster.
#
# The filter is here rather than in the probe's predicate because a tracepoint's
# array field can only be referenced once per BPF program: the second reference
# makes bpftrace materialise a pointer into the context, and the verifier rejects
# that with "dereference of modified ctx ptr". Comparing address bytes needs
# several references, and ntop() needs one more to print. So the probe emits
# every connection and the noise is dropped a pipe later.
set -uo pipefail

exec bpftrace -B line "$1" \
  | grep --line-buffered -Ev \
    ' daddr=(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|f[cd]|fe80:|::1 )'
